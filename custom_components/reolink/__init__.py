"""Custom Reolink wrapper that swaps the playback proxy for go2rtc."""

from __future__ import annotations

import logging

import homeassistant.components.reolink as core_reolink
import homeassistant.components.reolink.views as core_views

from .views import (
    Go2rtcPlaybackProxyView,
    Go2rtcProxyView,
    async_generate_playback_proxy_url,
)
from .proxy_view import (
    ReolinkFfmpegHlsStreamView,
    ReolinkFfmpegHlsView,
    ReolinkMp4ProxyView,
)

_LOGGER = logging.getLogger(__name__)

# Re-export unchanged pieces from the core integration so this custom component
# only overrides the playback view.
async_setup = core_reolink.async_setup
async_unload_entry = core_reolink.async_unload_entry
async_remove_entry = core_reolink.async_remove_entry
async_remove_config_entry_device = core_reolink.async_remove_config_entry_device
migrate_entity_ids = core_reolink.migrate_entity_ids


async def async_setup_entry(
    hass: core_reolink.HomeAssistant, config_entry: core_reolink.ReolinkConfigEntry
) -> bool:
    """Set up Reolink while swapping in the go2rtc-backed playback proxy."""
    _LOGGER.info("Custom Reolink setup entry started")

    # Swap the view used inside the core integration before delegating to it.
    core_reolink.PlaybackProxyView = Go2rtcPlaybackProxyView
    core_views.PlaybackProxyView = Go2rtcPlaybackProxyView
    core_views.async_generate_playback_proxy_url = async_generate_playback_proxy_url
    _LOGGER.debug("Reolink playback proxy patched to use go2rtc")

    result = await core_reolink.async_setup_entry(hass, config_entry)
    if result:
        hass.http.register_view(Go2rtcProxyView(hass))
        hass.http.register_view(ReolinkFfmpegHlsView(hass))
        hass.http.register_view(ReolinkFfmpegHlsStreamView(hass))
        hass.http.register_view(ReolinkMp4ProxyView(hass))
        _LOGGER.info("Registered go2rtc and ffmpeg playback proxy views")
    return result
