## Reolink Apple MP4 playback

This custom component delegates everything to the core Reolink integration but replaces the playback for Apple devices (`/api/reolink/video/...`) so Home Assistant fetches VOD through ffmpeg and serves a remuxed MP4 instead of pulling directly from the camera.

Restart Home Assistant after dropping this folder into `config/custom_components` to apply the override.
