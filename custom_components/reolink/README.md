## Reolink Apple HLS playback

This custom component delegates everything to the core Reolink integration but replaces the playback for Apple devices (`/api/reolink/video/...`) so Home Assistant fetches VOD through ffmpeg instead of directly from the camera.

Restart Home Assistant after dropping this folder into `config/custom_components` to apply the override.
