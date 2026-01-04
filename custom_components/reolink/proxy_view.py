"""Proxy Reolink VOD through HLS and MP4 Range endpoints."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from http import HTTPStatus
import logging
import os
from pathlib import Path
import secrets
import time
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
HLS_TIME = 1
HLS_LIST_SIZE = 4
HLS_SEGMENT_TYPE = "fmp4"
HLS_FLAGS = "append_list+omit_endlist+independent_segments"
HLS_READY_TIMEOUT = float(os.getenv("REOLINK_HLS_READY_TIMEOUT", "20"))
STREAM_TTL_SECONDS = 300
DEFAULT_TRANSCODE = os.getenv("REOLINK_HLS_TRANSCODE", "0") not in (
    "0",
    "false",
    "False",
    "",
    None,
)
IGNORE_RANGE = os.getenv("REOLINK_MP4_IGNORE_RANGE", "1") not in (
    "0",
    "false",
    "False",
    "",
    None,
)

STREAMS: dict[str, dict[str, object]] = {}
STREAMS_LOCK = asyncio.Lock()
_CLEANUP_TASK: asyncio.Task | None = None
LENGTH_CACHE: dict[str, int] = {}
LENGTH_CACHE_LOCK = asyncio.Lock()
SIZE_CACHE: dict[str, int] = {}
SIZE_CACHE_LOCK = asyncio.Lock()


async def _log_ffmpeg_stderr(proc: asyncio.subprocess.Process, token: str) -> None:
    if proc.stderr is None:
        return
    while True:
        line = await proc.stderr.readline()
        if not line:
            break
        _LOGGER.warning("ffmpeg[%s] %s", token, line.decode(errors="replace").rstrip())


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


def _stream_root(hass: HomeAssistant) -> Path:
    return Path(hass.config.path("reolink_hls"))


async def _ensure_cleanup_task(hass: HomeAssistant) -> None:
    global _CLEANUP_TASK
    if _CLEANUP_TASK is not None and not _CLEANUP_TASK.done():
        return

    async def _cleanup_loop() -> None:
        while True:
            await asyncio.sleep(10)
            now = time.time()
            stale: list[str] = []
            async with STREAMS_LOCK:
                for token, data in STREAMS.items():
                    if now - data["last_access"] > STREAM_TTL_SECONDS:
                        stale.append(token)
            for token in stale:
                await _stop_stream(token)

    _CLEANUP_TASK = hass.loop.create_task(_cleanup_loop())


async def _stop_stream(token: str) -> None:
    async with STREAMS_LOCK:
        data = STREAMS.pop(token, None)
    if not data:
        return

    _LOGGER.info("Stopping HLS stream %s", token)
    proc: asyncio.subprocess.Process = data["process"]
    if proc.returncode is None:
        proc.terminate()
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=3)
    if proc.returncode is None:
        proc.kill()
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=1)

    stream_dir: Path = data["dir"]
    with suppress(OSError):
        for item in stream_dir.glob("*"):
            with suppress(OSError):
                item.unlink()
        stream_dir.rmdir()


async def _spawn_hls(hass: HomeAssistant, clip_url: str, transcode: bool) -> str:
    token = secrets.token_hex(8)
    stream_dir = _stream_root(hass) / token
    stream_dir.mkdir(parents=True, exist_ok=True)

    playlist = stream_dir / "index.m3u8"
    segment_pattern = stream_dir / "seg%03d.m4s"
    init_name = "init.mp4"

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        clip_url,
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-f",
        "hls",
        "-hls_time",
        str(HLS_TIME),
        "-hls_list_size",
        str(HLS_LIST_SIZE),
        "-hls_flags",
        HLS_FLAGS,
        "-hls_segment_type",
        HLS_SEGMENT_TYPE,
        "-hls_fmp4_init_filename",
        init_name,
        "-hls_segment_filename",
        str(segment_pattern),
        str(playlist),
    ]
    insert_at = cmd.index("-f")
    if transcode:
        cmd[insert_at:insert_at] = [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "22",
            "-profile:v",
            "high",
            "-level",
            "4.1",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "100",
            "-keyint_min",
            "100",
            "-sc_threshold",
            "0",
            "-c:a",
            "aac",
            "-ac",
            "2",
            "-ar",
            "44100",
            "-b:a",
            "128k",
        ]
    else:
        cmd[insert_at:insert_at] = [
            "-c:v",
            "copy",
            "-c:a",
            "copy",
        ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    asyncio.create_task(_log_ffmpeg_stderr(process, token))

    async with STREAMS_LOCK:
        STREAMS[token] = {
            "dir": stream_dir,
            "process": process,
            "last_access": time.time(),
        }

    _LOGGER.info("Started HLS stream %s for %s", token, clip_url)
    return token


async def _wait_hls_ready(
    data: dict[str, object], filename: str
) -> tuple[Path | None, int, str | None]:
    file_path = Path(data["dir"]) / filename
    proc: asyncio.subprocess.Process = data["process"]

    start_wait = time.time()
    while time.time() - start_wait < HLS_READY_TIMEOUT:
        if file_path.is_file():
            break
        if proc.returncode is not None:
            return None, 502, f"ffmpeg exited early (code {proc.returncode})"
        await asyncio.sleep(0.2)
    if not file_path.is_file():
        return None, 503, "HLS output not ready"

    if filename.endswith(".m3u8"):
        start_wait = time.time()
        while time.time() - start_wait < HLS_READY_TIMEOUT:
            content = await asyncio.to_thread(file_path.read_text)
            segments = [line for line in content.splitlines() if line and not line.startswith("#")]
            if len(segments) >= 1:
                break
            await asyncio.sleep(0.2)
        else:
            return None, 503, "Playlist not ready"

    return file_path, 200, None


class ReolinkFfmpegHlsView(HomeAssistantView):
    """Proxy Reolink clips through ffmpeg and serve as HLS."""

    requires_auth = True
    url = "/api/reolink_proxy/hls/{identifier:.*}"
    name = "api:reolink_proxy_hls"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, identifier: str) -> web.StreamResponse:
        """Fetch a Reolink clip and re-mux it for HLS playback."""
        _LOGGER.info("Reolink ffmpeg HLS proxy request received")
        try:
            _LOGGER.debug("Reolink ffmpeg HLS proxy identifier=%s", identifier)
            clip_url, _vod_type, _ = await _resolve_clip_url(self.hass, identifier)
        except (ValueError, Unresolvable, ReolinkError) as err:
            _LOGGER.warning("Reolink ffmpeg HLS bad identifier: %s", err)
            return web.Response(text=str(err), status=HTTPStatus.BAD_REQUEST)
        _LOGGER.info(
            "Reolink ffmpeg HLS opening clip url=%s",
            clip_url,
        )

        await _ensure_cleanup_task(self.hass)
        transcode = request.query.get("transcode")
        if transcode is None:
            transcode_flag = DEFAULT_TRANSCODE
        else:
            transcode_flag = transcode not in ("0", "false", "False", "", None)
        try:
            token = await _spawn_hls(self.hass, clip_url, transcode_flag)
        except FileNotFoundError:
            return web.Response(
                text="ffmpeg not found in PATH",
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

        async with STREAMS_LOCK:
            data = STREAMS.get(token)
            if data:
                data["last_access"] = time.time()
        if not data:
            return web.Response(
                text="Failed to start HLS stream",
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        file_path, status, message = await _wait_hls_ready(data, "index.m3u8")
        if not file_path:
            return web.Response(text=message or "Playlist not ready", status=status)

        base_path = f"/api/reolink_proxy/hls_stream/{token}/"
        base_url = str(request.url.with_path(base_path).with_query(""))
        auth_suffix = f"?{request.query_string}" if request.query_string else ""
        playlist = await asyncio.to_thread(file_path.read_text)
        rewritten: list[str] = []
        for line in playlist.splitlines():
            if not line.strip():
                rewritten.append(line)
                continue
            if line.startswith("#EXT-X-MAP:URI="):
                uri_part = line.split("URI=", 1)[-1].strip()
                if uri_part.startswith("\"") and "\"" in uri_part[1:]:
                    uri = uri_part.split("\"", 2)[1]
                    if not uri.startswith(("http://", "https://", "/")):
                        line = line.replace(uri, base_url + uri + auth_suffix)
                rewritten.append(line)
                continue
            if line.startswith("#"):
                rewritten.append(line)
                continue
            rewritten.append(base_url + line + auth_suffix)
        body = "\n".join(rewritten).encode()
        headers = {
            "Content-Type": "application/vnd.apple.mpegurl",
            "Cache-Control": "no-store",
            "Content-Length": str(len(body)),
        }
        return web.Response(body=body, headers=headers)


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
class ReolinkFfmpegHlsStreamView(HomeAssistantView):
    """Serve HLS playlists/segments generated by ffmpeg."""

    requires_auth = False
    url = "/api/reolink_proxy/hls_stream/{token}/{filename:.*}"
    name = "api:reolink_proxy_hls_stream"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, token: str, filename: str) -> web.StreamResponse:
        async with STREAMS_LOCK:
            data = STREAMS.get(token)
        if not data:
            return web.Response(text="HLS stream not found", status=HTTPStatus.NOT_FOUND)

        data["last_access"] = time.time()
        file_path, status, message = await _wait_hls_ready(data, filename)
        if not file_path:
            return web.Response(text=message or "Segment not ready", status=status)

        if filename.endswith(".m3u8"):
            content_type = "application/vnd.apple.mpegurl"
        elif filename.endswith((".m4s", ".mp4")):
            content_type = "video/mp4"
        else:
            content_type = "application/octet-stream"

        headers = {"Content-Type": content_type, "Cache-Control": "no-store"}
        return web.FileResponse(path=file_path, headers=headers)


__all__ = [
    "ReolinkFfmpegHlsView",
    "ReolinkFfmpegHlsStreamView",
    "ReolinkMp4ProxyView",
    "generate_ffmpeg_proxy_url",
]
