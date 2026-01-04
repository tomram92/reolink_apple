## Reolink go2rtc playback interceptor

This custom component delegates everything to the core Reolink integration but replaces the playback proxy (`/api/reolink/video/...`) so Home Assistant fetches VOD through go2rtc instead of directly from the camera.

Configure the go2rtc endpoint with an environment variable:

```
REOLINK_GO2RTC_PROXY_URL=http://192.168.8.10:4002/hls
```

Restart Home Assistant after dropping this folder into `config/custom_components` to apply the override.
