SRT Streamer for Windows (x64)
==============================

Double-click "SRT Streamer.exe". Nothing to install: FFmpeg (with SRT and GPU screen
capture) is built in.

- First launch: Windows SmartScreen may warn because the exe isn't code-signed.
  Click "More info" -> "Run anyway". Startup takes a few seconds while it unpacks.
- To use a different FFmpeg, put ffmpeg.exe next to SRT Streamer.exe, or set it in
  the Advanced tab.
- Computer sound: pick "Computer sound: <your speakers/headphones>" as the audio
  source. It captures whatever plays through that output; no extra drivers needed.
- Settings are saved in %USERPROFILE%\.srt_streamer.json (the SRT passphrase is
  stored in plain text).

Test receiver on another PC:  ffplay "srt://0.0.0.0:9000?mode=listener"

FFmpeg is licensed under the GPL; see FFMPEG-LICENSE.txt. Its source is available
from https://ffmpeg.org and the build scripts from https://github.com/BtbN/FFmpeg-Builds.
