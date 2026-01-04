"""Custom Reolink wrapper that swaps the playback proxy for MP4 range streaming."""

from __future__ import annotations

import logging

import homeassistant.components.reolink as core_reolink

from .proxy_view import ReolinkMp4ProxyView

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
    """Set up Reolink while swapping in the MP4 playback proxy."""
    _LOGGER.info("Custom Reolink setup entry started")
    result = await core_reolink.async_setup_entry(hass, config_entry)
    if result:
        hass.http.register_view(ReolinkMp4ProxyView(hass))
        _LOGGER.info("Registered MP4 playback proxy view")
    return result
