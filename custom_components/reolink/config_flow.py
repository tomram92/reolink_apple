"""Config flow shim that reuses the core Reolink logic."""

from __future__ import annotations

# Import the core flow classes directly so Home Assistant can load the config flow
# even though this is a custom integration override.
from homeassistant.components.reolink.config_flow import (  # noqa: F401
    ReolinkFlowHandler,
    ReolinkOptionsFlowHandler,
)

__all__ = ["ReolinkFlowHandler", "ReolinkOptionsFlowHandler"]
