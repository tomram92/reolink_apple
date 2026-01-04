"""Custom playback proxy that routes Reolink VOD through go2rtc."""

from __future__ import annotations

import logging
from http import HTTPStatus
import os
from urllib.parse import quote, urlsplit, urlunsplit

from aiohttp import ClientError, ClientTimeout, web, ClientConnectorSSLError
from aiohttp.hdrs import CACHE_CONTROL
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.media_source import Unresolvable
from homeassistant.components.reolink.views import (
    PlaybackProxyView as CorePlaybackProxyView,
    async_generate_playback_proxy_url as core_async_generate_playback_proxy_url,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util.ssl import SSLCipherList
from urllib.parse import urljoin

_LOGGER = logging.getLogger(__name__)


def _force_https(url: str) -> str:
    """Ensure the URL uses https when it is absolute."""
    parts = urlsplit(url)
    if parts.scheme != "http":
        return url
    return urlunsplit(("https", parts.netloc, parts.path, parts.query, parts.fragment))


def async_generate_playback_proxy_url(*args, **kwargs) -> str:
    """Wrap the core generator and force https in the returned proxy URL."""
    url = core_async_generate_playback_proxy_url(*args, **kwargs)
    return _force_https(url)


class Go2rtcProxyView(HomeAssistantView):
    """Proxy go2rtc responses through HA to avoid mixed content."""

    requires_auth = True
    url = "/api/reolink/go2rtc_proxy"
    name = "api:reolink_go2rtc_proxy"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        # Session that allows HTTPS (certs ignored) and bypasses proxy env
        self.session = async_get_clientsession(
            hass, verify_ssl=False, ssl_cipher=SSLCipherList.INSECURE
        )

    async def get(self, request: web.Request) -> web.StreamResponse:
        src = request.query.get("src")
        if not src:
            return web.Response(status=400, text="missing src")

        headers = dict(request.headers)
        headers.pop("Host", None)
        headers.pop("Referer", None)

        try:
            upstream = await self.session.get(src, headers=headers, ssl=False)
        except ClientError as err:
            return web.Response(
                status=HTTPStatus.BAD_GATEWAY, text=f"go2rtc proxy error: {err!s}"
            )

        response_headers = dict(upstream.headers)
        if CACHE_CONTROL in response_headers:
            # avoid HA caching
            response_headers[CACHE_CONTROL] = "no-store, no-cache"

        # If this is an HLS playlist, rewrite segment URLs to this proxy
        content_type = upstream.headers.get("Content-Type", "")
        if "mpegurl" in content_type or upstream.url.path.endswith(".m3u8"):
            body = await upstream.text()
            base_url = str(upstream.url)
            base_dir = base_url.rsplit("/", 1)[0] + "/"
            rewritten: list[str] = []
            for line in body.splitlines():
                if line.startswith("#") or not line.strip():
                    rewritten.append(line)
                    continue
                absolute = line
                if not line.startswith("http://") and not line.startswith("https://"):
                    absolute = urljoin(base_dir, line)
                proxied = f"{self.url}?src={quote(absolute, safe='')}"
                rewritten.append(proxied)
            new_body = "\n".join(rewritten)
            response_headers["Content-Type"] = "application/vnd.apple.mpegurl"
            return web.Response(
                status=upstream.status,
                headers=response_headers,
                text=new_body,
            )

        # Otherwise stream the content through HA
        response = web.StreamResponse(
            status=upstream.status,
            reason=upstream.reason,
            headers=response_headers,
        )
        await response.prepare(request)
        try:
            async for chunk in upstream.content.iter_chunked(65536):
                await response.write(chunk)
        finally:
            upstream.release()
        await response.write_eof()
        return response


class Go2rtcPlaybackProxyView(CorePlaybackProxyView):
    """Playback proxy that proxies VOD through go2rtc instead of directly, using the HA frontend proxy."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the proxy."""
        # Rebuild the session to keep parity with the core implementation.
        self.hass = hass
        # self.session is no longer strictly needed for streaming data, but kept for token retry logic
        self.session = async_get_clientsession(
            hass,
            verify_ssl=False,
            ssl_cipher=SSLCipherList.INSECURE,
        )
        self._proxy_session = async_get_clientsession(
            hass,
            verify_ssl=False,
            ssl_cipher=SSLCipherList.INSECURE,
        )
        self._vod_type: str | None = None
        # go2rtc base URL should be absolute if possible (e.g., http://192.168.8.10:4002/hls)
        self._go2rtc_base = os.getenv("REOLINK_GO2RTC_PROXY_URL", "http://127.0.0.1:1984/hls")

    def _build_go2rtc_url(self, reolink_url: str, request: web.Request) -> str:
        """Encode the Reolink VOD URL as src= for go2rtc."""
        # Percent-encode the entire Reolink URL so go2rtc sees a fully encoded `src`
        encoded = quote(reolink_url, safe="")
        base = self._go2rtc_base
        # This logic is kept to ensure the URL is correctly constructed.
        # It's what the HA frontend proxy will see and route.
        # It no longer rewrites to the HA origin to avoid double-proxying.
        separator = "&" if "?" in base else "?"
        final = f"{base}{separator}src={encoded}"
        _LOGGER.warning(
            "go2rtc proxy URL built base=%s src_len=%s final=%s",
            base,
            len(reolink_url),
            final,
        )
        return final

    async def get(  # type: ignore[override]
        self,
        request: web.Request,
        config_entry_id: str,
        channel: str,
        stream_res: str,
        vod_type: str,
        filename: str,
        retry: int = 2,
    ) -> web.StreamResponse:
        """Get playback proxy video response routed through go2rtc."""
        from reolink_aio.enums import VodRequestType  # local import to avoid dependency issues
        from homeassistant.components.reolink.util import get_host  # imported lazily

        retry -= 1
        _LOGGER.warning(
            "go2rtc playback view invoked: entry=%s channel=%s stream=%s vod_type=%s filename_b64=%s retry=%s",
            config_entry_id,
            channel,
            stream_res,
            vod_type,
            filename,
            retry,
        )

        filename_decoded = self._decode_filename(filename)
        ch = int(channel)
        if self._vod_type is not None:
            vod_type = self._vod_type
        try:
            host = get_host(self.hass, config_entry_id)
        except Unresolvable:
            err_str = f"Reolink playback proxy could not find config entry id: {config_entry_id}"
            _LOGGER.warning(err_str)
            return web.Response(text=err_str, status=400)

        # Force DOWNLOAD mode to always request MP4 (avoid FLV playback responses)
        vod_mode = VodRequestType.DOWNLOAD

        # 1. Get the direct Reolink stream URL and token
        try:
            _mime_type, reolink_url = await host.api.get_vod_source(
                ch, filename_decoded, stream_res, vod_mode
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Reolink playback proxy error: %s", err)
            return web.Response(text=str(err), status=400)

        # 2. Build the final go2rtc URL (e.g., http://go2rtc:4002/hls?src=...)
        go2rtc_url = self._build_go2rtc_url(reolink_url, request)

        _LOGGER.warning(
            "Redirecting VOD to go2rtc url=%s camera=%s file=%s vod_mode=%s",
            go2rtc_url,
            host.api.camera_name(ch),
            filename_decoded,
            vod_mode.value,
        )

        # 3. Proxy the go2rtc content through HA to avoid mixed content and auth issues
        return await self._proxy_go2rtc(request, go2rtc_url)

    # The following methods are no longer needed for streaming but kept for completeness
    def _decode_filename(self, filename: str) -> str:
        """Decode the filename the same way as the core view."""
        from base64 import urlsafe_b64decode

        return urlsafe_b64decode(filename.encode("utf-8")).decode("utf-8")

    def _clean_headers(self, headers: dict[str, str]) -> dict[str, str]:
        """Remove headers that should not be forwarded."""
        proxy_headers = dict(headers)
        proxy_headers.pop("Host", None)
        proxy_headers.pop("Referer", None)
        return proxy_headers

    async def _proxy_go2rtc(
        self, request: web.Request, url: str
    ) -> web.StreamResponse:
        """Stream go2rtc content through HA (rewrite HLS playlists)."""
        headers = self._clean_headers(request.headers)
        try:
            upstream = await self._proxy_session.get(url, headers=headers, ssl=False)
        except ClientError as err:
            err_str = f"go2rtc proxy error: {err!s}"
            _LOGGER.warning(err_str)
            return web.Response(status=HTTPStatus.BAD_GATEWAY, text=err_str)

        response_headers = dict(upstream.headers)
        content_type = upstream.headers.get("Content-Type", "")

        _LOGGER.warning(
            "go2rtc upstream status=%s type=%s url=%s",
            upstream.status,
            content_type,
            upstream.url,
        )

        if upstream.status >= 400:
            try:
                body_preview = await upstream.text()
            except UnicodeDecodeError:
                body_preview = (await upstream.read()).decode("utf-8", "replace")
            _LOGGER.warning(
                "go2rtc upstream returned %s %s, body: %s",
                upstream.status,
                upstream.reason,
                body_preview[:200],
            )
            return web.Response(
                status=HTTPStatus.BAD_GATEWAY,
                text=f"go2rtc upstream error: {upstream.status} {upstream.reason}",
            )

        # Rewrite HLS playlists so segment URLs also go through this proxy
        if "mpegurl" in content_type or upstream.url.path.endswith(".m3u8"):
            body = await upstream.text()
            base_url = str(upstream.url)
            base_dir = base_url.rsplit("/", 1)[0] + "/"
            rewritten: list[str] = []
            for line in body.splitlines():
                if line.startswith("#") or not line.strip():
                    rewritten.append(line)
                    continue
                absolute = line
                if not line.startswith("http://") and not line.startswith("https://"):
                    absolute = urljoin(base_dir, line)
                proxied = f"{Go2rtcProxyView.url}?src={quote(absolute, safe='')}"
                rewritten.append(proxied)
            new_body = "\n".join(rewritten)
            response_headers["Content-Type"] = "application/vnd.apple.mpegurl"
            await upstream.release()
            return web.Response(
                status=upstream.status,
                headers=response_headers,
                text=new_body,
            )

        # Otherwise stream through HA
        response = web.StreamResponse(
            status=upstream.status,
            reason=upstream.reason,
            headers=response_headers,
        )
        await response.prepare(request)
        try:
            async for chunk in upstream.content.iter_chunked(65536):
                await response.write(chunk)
        finally:
            upstream.release()
        await response.write_eof()
        return response

    def _http_fallback(self, url: str) -> str | None:
        """Return an http version of the url if scheme is https."""
        if url.lower().startswith("https://"):
            return "http://" + url[len("https://") :]
        return None


__all__ = ["Go2rtcPlaybackProxyView", "async_generate_playback_proxy_url"]
