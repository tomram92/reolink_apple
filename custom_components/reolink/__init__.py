"""Custom Reolink wrapper with additional proxy views."""

from __future__ import annotations

import logging

import homeassistant.components.reolink as core_reolink
from .proxy_view import (
    ReolinkFfmpegMp4StreamView,
    ReolinkFfmpegMp4View,
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
    """Set up Reolink with extra Apple endpoints."""
    result = await core_reolink.async_setup_entry(hass, config_entry)
    if result:
        hass.http.register_view(ReolinkFfmpegMp4View(hass))
        hass.http.register_view(ReolinkFfmpegMp4StreamView(hass))
        _LOGGER.info("Registered Apple MP4 playback")
    return result
