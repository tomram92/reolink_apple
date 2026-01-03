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
from urllib.parse import quote, unquote

from aiohttp import web
from reolink_aio.enums import VodRequestType
from reolink_aio.exceptions import ReolinkError

from homeassistant.components.http import HomeAssistantView
from homeassistant.components.media_source import Unresolvable
from homeassistant.core import HomeAssistant

from .util import get_host

_LOGGER = logging.getLogger(__name__)

HLS_TIME = 1
HLS_LIST_SIZE = 4
HLS_SEGMENT_TYPE = os.getenv("REOLINK_HLS_SEGMENT_TYPE", "fmp4")
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

STREAMS: dict[str, dict[str, object]] = {}
STREAMS_LOCK = asyncio.Lock()
_CLEANUP_TASK: asyncio.Task | None = None


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
) -> tuple[str, int, str, str, str, str]:
    """Parse a Reolink media identifier in FILE|... format."""
    parts = identifier.split("|", 7)
    if len(parts) not in (7, 8) or parts[0] != "FILE":
        raise ValueError(f"Unsupported identifier: {identifier}")
    _, config_entry_id, channel_str, stream_res, filename, start_time, end_time = parts[:7]
    return config_entry_id, int(channel_str), stream_res, filename, start_time, end_time


def _vod_type_for_file(host, filename: str) -> VodRequestType:
    """Match the core Reolink VOD selection logic."""
    if filename.endswith((".mp4", ".vref")) or host.api.is_hub:
        if host.api.is_nvr:
            return VodRequestType.DOWNLOAD
        return VodRequestType.PLAYBACK
    if host.api.is_nvr:
        return VodRequestType.NVR_DOWNLOAD
    return VodRequestType.RTMP


def generate_ffmpeg_hls_url(identifier: str) -> str:
    """Build the HLS proxy URL for a media identifier."""
    encoded = quote(identifier, safe="")
    return f"/api/reolink_proxy/hls/{encoded}"


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
    }
    return clip_url, vod_type, info


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
    segment_type = HLS_SEGMENT_TYPE.lower()
    if segment_type in ("ts", "mpegts"):
        ffmpeg_segment_type = "mpegts"
        segment_pattern = stream_dir / "seg%03d.ts"
        init_name = None
    else:
        ffmpeg_segment_type = "fmp4"
        segment_pattern = stream_dir / "seg%03d.m4s"
        init_name = "init.mp4"

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
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
        ffmpeg_segment_type,
    ]
    if init_name:
        cmd += [
            "-hls_fmp4_init_filename",
            init_name,
        ]
    cmd += [
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
            message = f"ffmpeg exited early (code {proc.returncode})"
            return None, 502, message
        await asyncio.sleep(0.2)
    if not file_path.is_file():
        return None, 503, "HLS output not ready"

    if filename.endswith(".m3u8"):
        start_wait = time.time()
        while time.time() - start_wait < HLS_READY_TIMEOUT:
            content = await asyncio.to_thread(file_path.read_text)
            segments: list[str] = []
            init_file: str | None = None
            for line in content.splitlines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("#EXT-X-MAP:URI="):
                    uri_part = line.split("URI=", 1)[-1].strip()
                    if uri_part.startswith("\"") and "\"" in uri_part[1:]:
                        init_file = uri_part.split("\"", 2)[1]
                    else:
                        init_file = uri_part
                    continue
                if line.startswith("#"):
                    continue
                segments.append(line)
            ready = len(segments) >= 2
            paths_to_check: list[Path] = []
            if init_file:
                paths_to_check.append(Path(data["dir"]) / init_file)
            if ready:
                paths_to_check.extend(Path(data["dir"]) / seg for seg in segments[:2])
            if ready and any(not path.is_file() for path in paths_to_check):
                ready = False
            if ready:
                break
            await asyncio.sleep(0.2)
        else:
            return None, 503, "Playlist not ready"

    last_size = -1
    stable_start = time.time()
    while time.time() - stable_start < 2:
        try:
            size = file_path.stat().st_size
        except OSError:
            size = -1
        if size == last_size and size > 0:
            break
        last_size = size
        await asyncio.sleep(0.05)

    return file_path, 200, None


class ReolinkFfmpegHlsView(HomeAssistantView):
    """Proxy Reolink clips through ffmpeg and serve as HLS."""

    # Match MP4 proxy: Safari is redirected here and may not include auth headers.
    requires_auth = False
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
        auth_suffix = f"?{request.query_string}" if request.query_string else ""
        redirect_url = f"/api/reolink_proxy/hls_stream/{token}/index.m3u8{auth_suffix}"
        raise web.HTTPFound(location=redirect_url)

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
        elif filename.endswith(".ts"):
            content_type = "video/MP2T"
        else:
            content_type = "application/octet-stream"

        headers = {"Content-Type": content_type, "Cache-Control": "no-store"}
        if request.headers.get("Range"):
            body = await asyncio.to_thread(file_path.read_bytes)
            return web.Response(body=body, headers=headers)
        return web.FileResponse(path=file_path, headers=headers)


__all__ = [
    "ReolinkFfmpegHlsView",
    "ReolinkFfmpegHlsStreamView",
    "generate_ffmpeg_hls_url",
]
