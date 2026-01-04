"""Proxy Reolink VOD through MP4 Range endpoints."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from http import HTTPStatus
import logging
import os
import urllib.parse
from urllib.parse import quote, unquote

from aiohttp import ClientError, ClientTimeout, web
from reolink_aio.enums import VodRequestType
from reolink_aio.exceptions import ReolinkError

from homeassistant.components.http import HomeAssistantView
from homeassistant.components.media_source import Unresolvable
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .util import get_host

_LOGGER = logging.getLogger(__name__)

CHUNK_SIZE = 64 * 1024
IGNORE_RANGE = os.getenv("REOLINK_MP4_IGNORE_RANGE", "1") not in (
    "0",
    "false",
    "False",
    "",
    None,
)

LENGTH_CACHE: dict[str, int] = {}
LENGTH_CACHE_LOCK = asyncio.Lock()
SIZE_CACHE: dict[str, int] = {}
SIZE_CACHE_LOCK = asyncio.Lock()


def _parse_identifier(
    identifier: str,
) -> tuple[str, int, str, str, str, str, int | None]:
    """Parse a Reolink media identifier in FILE|... format."""
    _LOGGER.warning("Identifier to parse: %s", identifier)
    parts = identifier.split("|", 7)
    if len(parts) not in (7, 8) or parts[0] != "FILE":
        raise ValueError(f"Unsupported identifier: {identifier}")
    size = None
    if len(parts) == 8 and parts[7].isdigit():
        size = int(parts[7])
    _, config_entry_id, channel_str, stream_res, filename, start_time, end_time = parts[:7]
    return config_entry_id, int(channel_str), stream_res, filename, start_time, end_time, size


def _vod_type_for_file(host, filename: str) -> VodRequestType:
    """Match the core Reolink VOD selection logic."""
    if filename.endswith((".mp4", ".vref")) or host.api.is_hub:
        if host.api.is_nvr:
            return VodRequestType.DOWNLOAD
        return VodRequestType.PLAYBACK
    if host.api.is_nvr:
        return VodRequestType.NVR_DOWNLOAD
    return VodRequestType.RTMP


def generate_ffmpeg_proxy_url(identifier: str) -> str:
    """Build the MP4 proxy URL for a media identifier."""
    encoded = quote(identifier, safe="")
    _LOGGER.info("Reolink MP4 proxy URL generated")
    return f"/api/reolink_proxy/mp4/{encoded}"


def _parse_range_header(range_header: str) -> tuple[int | None, int | None] | None:
    if not range_header.startswith("bytes="):
        return None
    part = range_header.split("=", 1)[1].strip()
    if "," in part:
        return None
    start_str, end_str = part.split("-", 1)
    start = int(start_str) if start_str else None
    end = int(end_str) if end_str else None
    if start is None and end is None:
        return None
    return start, end


def _parse_content_range(value: str) -> tuple[int, int, int | None] | None:
    try:
        unit, rest = value.split(" ", 1)
        if unit != "bytes":
            return None
        range_part, total_part = rest.split("/", 1)
        start_str, end_str = range_part.split("-", 1)
        start = int(start_str)
        end = int(end_str)
        total = None if total_part.strip() == "*" else int(total_part)
        return start, end, total
    except Exception:
        return None


def _extract_length(headers) -> int | None:
    content_length = headers.get("Content-Length")
    if content_length and content_length.isdigit():
        return int(content_length)
    etag = headers.get("ETag") or headers.get("Etag") or headers.get("etag")
    if etag:
        etag = etag.strip("\"")
        if "-" in etag:
            suffix = etag.split("-")[-1]
            try:
                return int(suffix, 16)
            except ValueError:
                pass
    content_range = headers.get("Content-Range")
    if content_range:
        parsed = _parse_content_range(content_range)
        if parsed and parsed[2] is not None:
            return parsed[2]
    return None


async def _probe_length(session, url: str) -> int | None:
    async with LENGTH_CACHE_LOCK:
        cached = LENGTH_CACHE.get(url)
    if cached is not None:
        return cached

    headers = {"Accept-Encoding": "identity"}
    try:
        head = await session.head(url, headers=headers, ssl=False, timeout=ClientTimeout(total=10))
        length = _extract_length(head.headers)
        head.release()
        if length is not None:
            async with LENGTH_CACHE_LOCK:
                LENGTH_CACHE[url] = length
            return length
    except ClientError:
        pass

    try:
        resp = await session.get(
            url,
            headers={"Range": "bytes=0-1", "Accept-Encoding": "identity"},
            ssl=False,
            timeout=ClientTimeout(total=10),
        )
        length = _extract_length(resp.headers)
        resp.release()
        if length is not None:
            async with LENGTH_CACHE_LOCK:
                LENGTH_CACHE[url] = length
            return length
    except ClientError:
        pass
    return None


async def _resolve_clip_url(
    hass: HomeAssistant, identifier: str, *, force_download: bool = False
) -> tuple[str, VodRequestType, dict[str, object]] | None:
    decoded = unquote(identifier)
    (
        config_entry_id,
        channel,
        stream_res,
        filename,
        start_time,
        end_time,
        size,
    ) = _parse_identifier(decoded)
    host = get_host(hass, config_entry_id)
    if force_download:
        vod_type = VodRequestType.NVR_DOWNLOAD if host.api.is_nvr else VodRequestType.DOWNLOAD
    else:
        vod_type = _vod_type_for_file(host, filename)
    if vod_type == VodRequestType.NVR_DOWNLOAD:
        filename = f"{start_time}_{end_time}"
    if vod_type not in {
        VodRequestType.DOWNLOAD,
        VodRequestType.NVR_DOWNLOAD,
        VodRequestType.PLAYBACK,
    }:
        raise ValueError(f"Unsupported VOD type for proxy: {vod_type.value}")
    _mime_type, clip_url = await host.api.get_vod_source(
        channel, filename, stream_res, vod_type
    )
    info = {
        "channel": channel,
        "stream_res": stream_res,
        "filename": filename,
        "start_time": start_time,
        "end_time": end_time,
        "size": size,
    }
    return clip_url, vod_type, info


def _parse_time_dict(ts: str) -> dict[str, int] | None:
    if len(ts) != 14 or not ts.isdigit():
        return None
    return {
        "year": int(ts[0:4]),
        "mon": int(ts[4:6]),
        "day": int(ts[6:8]),
        "hour": int(ts[8:10]),
        "min": int(ts[10:12]),
        "sec": int(ts[12:14]),
    }


async def _search_file_size(
    session,
    clip_url: str,
    *,
    channel: int,
    stream_res: str,
    filename: str,
    start_time: str,
    end_time: str,
) -> int | None:
    cache_key = f"{clip_url}|{filename}"
    async with SIZE_CACHE_LOCK:
        cached = SIZE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    parsed = urllib.parse.urlsplit(clip_url)
    query = urllib.parse.parse_qs(parsed.query)
    token = (query.get("token") or [None])[0]
    if not token:
        return None

    start = _parse_time_dict(start_time)
    end = _parse_time_dict(end_time)
    if not start or not end:
        return None

    stream_type = "main" if stream_res.lower() in ("main", "high") else "sub"
    search_url = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, "/cgi-bin/api.cgi", f"cmd=Search&token={token}", "")
    )
    payload = [
        {
            "cmd": "Search",
            "action": 0,
            "param": {
                "Search": {
                    "channel": channel,
                    "onlyStatus": 0,
                    "streamType": stream_type,
                    "StartTime": start,
                    "EndTime": end,
                }
            },
        }
    ]
    try:
        resp = await session.post(
            search_url,
            json=payload,
            timeout=ClientTimeout(total=10),
            ssl=False,
        )
    except ClientError:
        return None

    try:
        data = await resp.json(content_type=None)
    except Exception:
        resp.release()
        return None
    resp.release()

    basename = os.path.basename(filename)
    entries = []
    try:
        entries = data[0]["value"]["SearchResult"].get("File", [])
    except Exception:
        return None

    for entry in entries:
        name = entry.get("name") or entry.get("fileName") or entry.get("filename")
        if not name:
            continue
        if name == basename or name in filename:
            size = entry.get("size") or entry.get("Size")
            if isinstance(size, int):
                async with SIZE_CACHE_LOCK:
                    SIZE_CACHE[cache_key] = size
                return size
    return None


class ReolinkMp4ProxyView(HomeAssistantView):
    """Proxy Reolink MP4 clips with Range support for Safari."""

    requires_auth = False
    url = "/api/reolink_proxy/mp4/{identifier:.*}"
    name = "api:reolink_proxy_mp4"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, identifier: str) -> web.StreamResponse:
        _LOGGER.info("Reolink MP4 proxy request received")
        try:
            clip_url, _vod_type, info = await _resolve_clip_url(
                self.hass, identifier, force_download=True
            )
        except (ValueError, Unresolvable, ReolinkError) as err:
            _LOGGER.warning("Reolink MP4 proxy bad identifier: %s", err)
            return web.Response(text=str(err), status=HTTPStatus.BAD_REQUEST)

        range_header = request.headers.get("Range")
        _LOGGER.warning(
            "Reolink MP4 proxy headers filename=%s range=%s",
            info.get("filename"),
            range_header,
        )
        _LOGGER.warning(
            "Reolink MP4 proxy info %s",
            ", ".join(f"{key}={value}" for key, value in info.items()),
        )
        size_param = request.query.get("size")
        size_hint = int(size_param) if size_param and size_param.isdigit() else None
        if size_hint is None and isinstance(info.get("size"), int):
            size_hint = info["size"]
        ignore_range = bool(range_header) and IGNORE_RANGE and size_hint is None
        forward_range = bool(range_header) and not ignore_range and size_hint is None
        if ignore_range:
            range_header = None
        parsed_range = _parse_range_header(range_header) if range_header else None
        if range_header and not parsed_range:
            return web.Response(text="Invalid Range header", status=HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)

        headers = {"Accept-Encoding": "identity"}
        if forward_range:
            headers["Range"] = range_header

        session = async_get_clientsession(self.hass)
        total_length = size_hint
        start = None
        end = None
        _LOGGER.warning(
            "Reolink MP4 size lookup filename=%s size=%s",
            info["filename"],
            total_length,
        )
        if parsed_range:
            start, end = parsed_range
            if total_length is None:
                total_length = await _search_file_size(
                    session,
                    clip_url,
                    channel=info["channel"],
                    stream_res=info["stream_res"],
                    filename=info["filename"],
                    start_time=info["start_time"],
                    end_time=info["end_time"],
                )
                _LOGGER.warning(
                    "Reolink MP4 size lookup filename=%s size=%s",
                    info["filename"],
                    total_length,
                )
            if total_length is None:
                total_length = await _probe_length(session, clip_url)
            if start is None and end is not None:
                if total_length is None:
                    return web.Response(
                        text="Cannot satisfy suffix range without length",
                        status=HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                    )
                start = max(total_length - end, 0)
                end = total_length - 1

        try:
            upstream = await session.get(
                clip_url,
                headers=headers,
                timeout=ClientTimeout(total=60),
                ssl=False,
            )
        except ClientError as err:
            _LOGGER.warning("Reolink MP4 proxy upstream error: %s", err)
            return web.Response(text=str(err), status=HTTPStatus.BAD_GATEWAY)

        content_type = upstream.headers.get("Content-Type", "video/mp4")
        upstream_cl = upstream.headers.get("Content-Length")
        if total_length is None and upstream_cl and upstream_cl.isdigit():
            total_length = int(upstream_cl)
        if total_length is None:
            total_length = _extract_length(upstream.headers)

        if range_header and upstream.status == HTTPStatus.OK and parsed_range and start is not None:
            end_byte = end if end is not None else (total_length - 1 if total_length else None)
            if total_length is not None and start >= total_length:
                upstream.release()
                return web.Response(status=HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            to_send = None
            if end_byte is not None:
                to_send = end_byte - start + 1

            response = web.StreamResponse(status=HTTPStatus.PARTIAL_CONTENT)
            response.content_type = content_type
            response.headers["Accept-Ranges"] = "bytes"
            response.headers["Cache-Control"] = "no-store"
            if total_length is not None and end_byte is not None:
                response.headers["Content-Range"] = f"bytes {start}-{end_byte}/{total_length}"
                response.headers["Content-Length"] = str(to_send)
            await response.prepare(request)

            read_pos = 0
            sent = 0
            try:
                async for chunk in upstream.content.iter_chunked(CHUNK_SIZE):
                    if not chunk:
                        break
                    chunk_start = read_pos
                    chunk_end = read_pos + len(chunk) - 1
                    read_pos += len(chunk)
                    if chunk_end < start:
                        continue
                    if chunk_start < start:
                        chunk = chunk[start - chunk_start :]
                        chunk_start = start
                    if end_byte is not None and chunk_start > end_byte:
                        break
                    if end_byte is not None and chunk_start + len(chunk) - 1 > end_byte:
                        chunk = chunk[: end_byte - chunk_start + 1]
                    await response.write(chunk)
                    sent += len(chunk)
                    if to_send is not None and sent >= to_send:
                        break
            finally:
                upstream.release()
                with suppress(RuntimeError, ConnectionResetError):
                    await response.write_eof()
            return response

        response_status = upstream.status
        if range_header and upstream.status == HTTPStatus.PARTIAL_CONTENT:
            response_status = HTTPStatus.PARTIAL_CONTENT
        if ignore_range:
            response_status = HTTPStatus.OK

        response = web.StreamResponse(status=response_status)
        response.content_type = content_type
        if not ignore_range:
            response.headers["Accept-Ranges"] = "bytes"
        response.headers["Cache-Control"] = "no-store"
        upstream_cr = upstream.headers.get("Content-Range")
        if not ignore_range and range_header and start is not None and total_length is not None:
            end_byte = end if end is not None else total_length - 1
            response.headers["Content-Range"] = f"bytes {start}-{end_byte}/{total_length}"
        elif not ignore_range and upstream_cr:
            response.headers["Content-Range"] = upstream_cr
        if upstream_cl:
            response.headers["Content-Length"] = upstream_cl
        await response.prepare(request)
        try:
            async for chunk in upstream.content.iter_chunked(CHUNK_SIZE):
                await response.write(chunk)
        finally:
            upstream.release()
            with suppress(RuntimeError, ConnectionResetError):
                await response.write_eof()
        return response


__all__ = [
    "ReolinkMp4ProxyView",
    "generate_ffmpeg_proxy_url",
]
