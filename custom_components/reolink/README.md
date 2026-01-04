## Reolink MP4 playback interceptor

This custom component delegates everything to the core Reolink integration but replaces the playback proxy (`/api/reolink/video/...`) with an MP4 range-capable proxy for VOD playback.

Restart Home Assistant after dropping this folder into `config/custom_components` to apply the override.
