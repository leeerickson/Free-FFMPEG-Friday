SRT Streamer for Windows (x64)
==============================

Unzip the whole "SRT Streamer" folder anywhere (for example Documents or C:\Tools) and
double-click "SRT Streamer.exe" inside it. Keep the "_internal" folder next to the exe: it holds
Python, FFmpeg (with SRT and GPU screen capture) and the SRT library. Nothing to install.

Two downloads:
- SRT-Streamer-Windows-x64.zip: the standard app. Streams with FFmpeg's SRT; the bandwidth
  graph shows encoder output.
- SRT-Streamer-Windows-x64-with-srt-stats.zip: adds the SRT library (libsrt) for detailed
  link statistics and the SRT link test. Until the app is code-signed, Windows Smart App
  Control may block it; if the library can't load, the app falls back to the standard mode.

If Windows says it can't verify the app (it isn't code-signed yet):
- Before unzipping: right-click the downloaded zip -> Properties -> tick "Unblock" -> OK.
  That clears the "downloaded from the internet" mark for everything inside.
- "Windows protected your PC": click "More info" -> "Run anyway".
- Windows Security / Defender alert: Protection history -> the entry -> Actions -> Allow on device.
- "Smart App Control blocked an app" (Windows 11) can't be overridden per app. Smart App
  Control only allows signed apps, so this needs a signed build (or Smart App Control turned
  off, which Windows can't undo without a reset).

- To use a different FFmpeg, put ffmpeg.exe next to SRT Streamer.exe, or set it in
  the Advanced tab.
- Computer sound: pick "Computer sound: <your speakers/headphones>" as the audio
  source. It captures whatever plays through that output; no extra drivers needed.
- Settings are saved in %USERPROFILE%\.srt_streamer.json (the SRT passphrase is
  stored in plain text).

Test receiver on another PC:  ffplay "srt://0.0.0.0:9000?mode=listener"

FFmpeg is licensed under the GPL; see FFMPEG-LICENSE.txt. Its source is available
from https://ffmpeg.org and the build scripts from https://github.com/BtbN/FFmpeg-Builds.
