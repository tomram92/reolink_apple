"""Media source override to route Reolink MP4 clips through a Range proxy."""

from __future__ import annotations

import datetime as dt
import logging
import urllib.parse

from reolink_aio.api import DUAL_LENS_MODELS
from reolink_aio.typings import VOD_trigger

from homeassistant.components.media_player import MediaClass, MediaType
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
# from homeassistant.components.media_player.const import STREAM_TYPE_HLS
import homeassistant.components.reolink.media_source as core_media_source

from .proxy_view import generate_ffmpeg_proxy_url
from .util import get_host

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

    def _sign_with_size(self, path: str, size: int | None) -> str:
        if size is None:
            return self._sign_path(path)
        signed = self._sign_path(path)
        if "authSig=" in signed:
            return signed
        base = self._sign_path(path.split("?", 1)[0])
        if "authSig=" not in base:
            return path
        parts = urllib.parse.urlsplit(base)
        query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        query["size"] = [str(size)]
        new_query = urllib.parse.urlencode(query, doseq=True)
        return urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path, new_query, parts.fragment)
        )

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        if not item.identifier:
            raise Unresolvable("Missing media identifier")

        _LOGGER.warning(
            "Reolink media_source identifier_raw %r",
            item.identifier,
        )
        parts = item.identifier.split("|", 7)
        if len(parts) not in (7, 8) or parts[0] != "FILE":
            _LOGGER.warning(
                "Reolink media_source non-file identifier %s",
                item.identifier,
            )
            return await super().async_resolve_media(item)

        size = None
        if len(parts) == 8 and parts[7].isdigit():
            size = int(parts[7])
        _LOGGER.warning("Reolink media_source parts %s", parts[6])
        _LOGGER.warning("Reolink media_source size %s", size)
        _LOGGER.warning("Reolink media_source resolved mp4 via Range proxy")
        _LOGGER.warning(
            "Reolink media_source identifier %s",
            item.identifier,
        )
        proxy_url = generate_ffmpeg_proxy_url(item.identifier)
        if size is not None:
            proxy_url = f"{proxy_url}?size={size}"
        proxy_url = self._sign_with_size(proxy_url, size)
        return PlayMedia(
            proxy_url,
            "video/mp4",
        )

    async def _async_generate_camera_files(
        self,
        config_entry_id: str,
        channel: int,
        stream: str,
        year: int,
        month: int,
        day: int,
        event: str | None = None,
    ) -> BrowseMediaSource:
        """Return all recording files on a specific day with size in identifier."""
        host = get_host(self.hass, config_entry_id)

        start = dt.datetime(year, month, day, hour=0, minute=0, second=0)
        end = dt.datetime(year, month, day, hour=23, minute=59, second=59)

        children: list[BrowseMediaSource] = []
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Requesting VODs of %s on %s/%s/%s",
                host.api.camera_name(channel),
                year,
                month,
                day,
            )
        event_trigger = VOD_trigger[event] if event is not None else None
        _, vod_files = await host.api.request_vod_files(
            channel,
            start,
            end,
            stream=stream,
            split_time=VOD_SPLIT_TIME,
            trigger=event_trigger,
        )

        if event is None and host.api.is_nvr and not host.api.is_hub:
            triggers = VOD_trigger.NONE
            for file in vod_files:
                triggers |= file.triggers

            children.extend(
                BrowseMediaSource(
                    domain=core_media_source.DOMAIN,
                    identifier=(
                        f"EVE|{config_entry_id}|{channel}|{stream}|"
                        f"{year}|{month}|{day}|{trigger.name}"
                    ),
                    media_class=MediaClass.DIRECTORY,
                    media_content_type=MediaType.PLAYLIST,
                    title=str(trigger.name).title(),
                    can_play=False,
                    can_expand=True,
                )
                for trigger in triggers
            )

        for file in vod_files:
            file_name = f"{file.start_time.time()} {file.duration}"
            if file.triggers != file.triggers.NONE:
                file_name += " " + " ".join(
                    str(trigger.name).title() for trigger in file.triggers
                )

            size = getattr(file, "size", None)
            if size is None:
                size = getattr(file, "file_size", None)
            if isinstance(size, str) and size.isdigit():
                size = int(size)
            size_part = str(size) if isinstance(size, int) else ""
            _LOGGER.warning("Reolink generate file size %s", size)
            identifier = (
                f"FILE|{config_entry_id}|{channel}|{stream}|{file.file_name}"
                f"|{file.start_time_id}|{file.end_time_id}"
            )
            if size_part:
                identifier = f"{identifier}|{size_part}"

            _LOGGER.warning(
                "Reolink generate identifier filename=%s size=%s id=%s",
                file.file_name,
                size,
                identifier,
            )
            children.append(
                BrowseMediaSource(
                    domain=core_media_source.DOMAIN,
                    identifier=identifier,
                    media_class=MediaClass.VIDEO,
                    media_content_type=MediaType.VIDEO,
                    title=file_name,
                    can_play=True,
                    can_expand=False,
                )
            )

        title = (
            f"{host.api.camera_name(channel)} "
            f"{core_media_source.res_name(stream)} {year}/{month}/{day}"
        )
        if host.api.model in DUAL_LENS_MODELS:
            title = (
                f"{host.api.camera_name(channel)} lens {channel} "
                f"{core_media_source.res_name(stream)} {year}/{month}/{day}"
            )
        if event:
            title = f"{title} {event.title()}"

        return BrowseMediaSource(
            domain=core_media_source.DOMAIN,
            identifier=f"FILES|{config_entry_id}|{channel}|{stream}",
            media_class=MediaClass.CHANNEL,
            media_content_type=MediaType.PLAYLIST,
            title=title,
            can_play=False,
            can_expand=True,
            children=children,
        )
