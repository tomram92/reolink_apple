"""Media source override to route Reolink clips through a remuxed MP4 proxy."""

from __future__ import annotations

import datetime as dt
import logging

from homeassistant.components.http import current_request
from homeassistant.components.media_source import (
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
import homeassistant.components.reolink.media_source as core_media_source

from .proxy_view import generate_ffmpeg_mp4_url

_LOGGER = logging.getLogger(__name__)
VOD_SPLIT_TIME = dt.timedelta(minutes=5)

async def async_get_media_source(hass):  # type: ignore[override]
    """Set up the Reolink media source with an MP4 proxy override."""
    return ReolinkVODMediaSource(hass)


class ReolinkVODMediaSource(core_media_source.ReolinkVODMediaSource):
    """Proxy MP4 media through the HA Range proxy."""

    def _sign_path(self, path: str) -> str:
        signer = getattr(self.hass.http, "async_sign_path", None)
        if callable(signer):
            return signer(path)
        signer = getattr(self.hass.http, "sign_path", None)
        if callable(signer):
            return signer(path)
        _LOGGER.warning("Reolink media_source could not sign path")
        return path

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        if not item.identifier:
            raise Unresolvable("Missing media identifier")

        parts = item.identifier.split("|", 7)
        if len(parts) not in (7, 8) or parts[0] != "FILE":
            return await super().async_resolve_media(item)

        proxy_url = self._sign_path(generate_ffmpeg_mp4_url(item.identifier))
        return PlayMedia(proxy_url, "video/mp4")

        return await super().async_resolve_media(item)
