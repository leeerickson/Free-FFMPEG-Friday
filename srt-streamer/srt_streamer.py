#!/usr/bin/env python3
"""
SRT Streamer
------------
Capture a screen or camera plus an audio source with FFmpeg and stream it over SRT,
with a live confidence preview, stereo audio meters and stream-health readouts.

Requirements: Python 3.8+ with Tkinter, and an FFmpeg build that includes libsrt.
Works on Windows (gdigrab/dshow), macOS (avfoundation) and Linux (x11grab/v4l2,
PulseAudio/PipeWire/ALSA).

Run:  python srt_streamer.py
"""
import collections
import json
import os
import platform
import queue
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time

SYSTEM = platform.system()
if SYSTEM == "Windows":
    # Portable runtime: point Tcl/Tk at the bundled script libraries before tkinter loads.
    for _var, _sub in (("TCL_LIBRARY", "tcl8.6"), ("TK_LIBRARY", "tk8.6")):
        _p = os.path.join(sys.base_prefix, "tcl", _sub)
        if _var not in os.environ and os.path.isdir(_p):
            os.environ[_var] = _p
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SRTStreamer.App")
    except Exception:
        pass

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox

APP_NAME = "SRT Streamer"
APP_VERSION = "1.2"
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".srt_streamer.json")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if SYSTEM == "Windows" else 0

ENCODERS = [
    "libx264  (H.264, CPU)",
    "h264_nvenc  (H.264, NVIDIA GPU)",
    "h264_qsv  (H.264, Intel Quick Sync)",
    "h264_amf  (H.264, AMD GPU)",
    "h264_videotoolbox  (H.264, Apple hardware)",
    "libx265  (HEVC, CPU)",
    "hevc_nvenc  (HEVC, NVIDIA GPU)",
]
ENCODER_ARGS = {
    "libx264": ["-preset", "veryfast", "-tune", "zerolatency"],
    "libx265": ["-preset", "veryfast", "-tune", "zerolatency"],
    "h264_nvenc": ["-preset", "p4", "-tune", "ll", "-rc", "cbr", "-bf", "0"],
    "hevc_nvenc": ["-preset", "p4", "-tune", "ll", "-rc", "cbr", "-bf", "0"],
    "h264_qsv": ["-preset", "veryfast"],
    "h264_amf": ["-usage", "lowlatency", "-rc", "cbr"],
    "h264_videotoolbox": ["-realtime", "1", "-allow_sw", "1"],
}
SCALES = {"Native": None, "1080p": 1080, "720p": 720, "540p": 540, "480p": 480}
MODE_HINTS = {
    "caller": "Push the stream to a receiver that is listening at Host : Port.",
    "listener": "Wait on Port for the receiver to connect to this computer. "
                "Host is optional (local address to bind).",
    "rendezvous": "Both ends connect to each other at the same time, useful when both "
                  "are behind NAT. Host is the other side's address.",
}
PRESETS = [
    ("720p30 · 2.5 Mbps", {"scale": "720p", "fps": "30", "vbitrate": "2500"}),
    ("1080p30 · 4.5 Mbps", {"scale": "1080p", "fps": "30", "vbitrate": "4500"}),
    ("1080p60 · 6 Mbps", {"scale": "1080p", "fps": "60", "vbitrate": "6000"}),
    ("Native · 8 Mbps", {"scale": "Native", "fps": "30", "vbitrate": "8000"}),
]
METER_FILTER = ("astats=metadata=1:reset=1,"
                "ametadata=mode=print:key=lavfi.astats.1.Peak_level,"
                "ametadata=mode=print:key=lavfi.astats.2.Peak_level")


def find_ffmpeg():
    """Prefer an ffmpeg next to the app, then one bundled inside the exe, then PATH."""
    exe = "ffmpeg.exe" if SYSTEM == "Windows" else "ffmpeg"
    here = os.path.dirname(os.path.abspath(sys.argv[0] or __file__))
    bases = [here, os.path.dirname(here)]
    if getattr(sys, "_MEIPASS", None):  # PyInstaller one-file bundle
        bases.append(sys._MEIPASS)
    for base in bases:
        for sub in ("", "ffmpeg", os.path.join("ffmpeg", "bin"), "bin"):
            cand = os.path.join(base, sub, exe)
            if os.path.isfile(cand):
                return cand
    return shutil.which("ffmpeg") or "ffmpeg"


def ffmpeg_exists(path):
    return bool(path) and (os.path.isfile(path) or shutil.which(path) is not None)


DEFAULTS = {
    "ffmpeg": find_ffmpeg(),
    "video": "",
    "audio": "None",
    "draw_mouse": True,
    "encoder": ENCODERS[0],
    "vbitrate": "4000",
    "fps": "30",
    "scale": "Native",
    "keyint": "2",
    "abitrate": "128",
    "mode": "caller",
    "host": "",
    "port": "9000",
    "latency": "200",
    "passphrase": "",
    "pbkeylen": "16",
    "streamid": "",
    "reconnect": True,
}


# ==========================================================================
# Device discovery
# ==========================================================================
def run_quiet(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           stdin=subprocess.DEVNULL, timeout=timeout, creationflags=NO_WINDOW)
        return (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.SubprocessError):
        return ""


def windows_monitors():
    import ctypes
    from ctypes import wintypes
    mons = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC,
                               ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def cb(hmon, _hdc, rect, _lp):
        r = rect.contents
        mons.append((r.left, r.top, r.right - r.left, r.bottom - r.top, hmon))
        return 1

    ctypes.windll.user32.EnumDisplayMonitors(None, None, proto(cb), 0)
    return mons


def dxgi_outputs():
    """Outputs of the default graphics adapter, in the order ddagrab's output_idx uses.

    Returns [(device_name, (left, top, right, bottom), hmonitor), ...]
    """
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    class DXGI_OUTPUT_DESC(ctypes.Structure):
        _fields_ = [("DeviceName", ctypes.c_wchar * 32), ("DesktopCoordinates", wintypes.RECT),
                    ("AttachedToDesktop", wintypes.BOOL), ("Rotation", ctypes.c_int),
                    ("Monitor", wintypes.HMONITOR)]

    def com(obj, index, *args, argtypes=()):
        vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtbl[index])
        return fn(obj, *args)

    iid_factory1 = GUID(0x770AAE78, 0xF26F, 0x4DBA,
                        (ctypes.c_ubyte * 8)(0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87))
    factory, adapter = ctypes.c_void_p(), ctypes.c_void_p()
    if ctypes.windll.dxgi.CreateDXGIFactory1(ctypes.byref(iid_factory1), ctypes.byref(factory)) < 0:
        return []
    outputs = []
    try:
        # IDXGIFactory::EnumAdapters (vtable 7): adapter 0 is the default device ddagrab uses
        if com(factory, 7, 0, ctypes.byref(adapter), argtypes=(ctypes.c_uint, ctypes.c_void_p)) < 0:
            return []
        try:
            i = 0
            while True:
                out = ctypes.c_void_p()
                # IDXGIAdapter::EnumOutputs (vtable 7)
                if com(adapter, 7, i, ctypes.byref(out), argtypes=(ctypes.c_uint, ctypes.c_void_p)) < 0:
                    break
                desc = DXGI_OUTPUT_DESC()
                # IDXGIOutput::GetDesc (vtable 7)
                if com(out, 7, ctypes.byref(desc), argtypes=(ctypes.c_void_p,)) >= 0:
                    rc = desc.DesktopCoordinates
                    outputs.append((desc.DeviceName, (rc.left, rc.top, rc.right, rc.bottom), desc.Monitor))
                com(out, 2)  # Release
                i += 1
        finally:
            com(adapter, 2)
    finally:
        com(factory, 2)
    return outputs


def parse_dshow(text):
    video, audio, section = [], [], None
    for line in text.splitlines():
        if "DirectShow video devices" in line:
            section = "video"
            continue
        if "DirectShow audio devices" in line:
            section = "audio"
            continue
        if "dshow" not in line or "Alternative name" in line:
            continue
        m = re.search(r'"([^"]+)"\s*(?:\((video|audio|none)\))?', line)
        if not m:
            continue
        kind = m.group(2) or section
        if kind == "video":
            video.append(m.group(1))
        elif kind == "audio":
            audio.append(m.group(1))
    return video, audio


def parse_avfoundation(text):
    video, audio, section = [], [], None
    for line in text.splitlines():
        if "AVFoundation video devices" in line:
            section = "video"
            continue
        if "AVFoundation audio devices" in line:
            section = "audio"
            continue
        m = re.search(r"\]\s*\[(\d+)\]\s*(.+)$", line)
        if m and section:
            (video if section == "video" else audio).append((int(m.group(1)), m.group(2).strip()))
    return video, audio


def enumerate_sources(ff):
    """Return (video_sources, audio_sources, notes). Each source is a dict with a 'label'."""
    video, audio, notes = [], [], []

    if SYSTEM == "Windows":
        filters = run_quiet([ff, "-hide_banner", "-filters"])
        has_dda = bool(re.search(r"^\s*\S+\s+ddagrab\s", filters, re.M))
        covered = set()
        if has_dda:
            try:
                for idx, (dev, (l, t, r, b), hmon) in enumerate(dxgi_outputs()):
                    main = " (main)" if (l, t) == (0, 0) else ""
                    video.append({"label": f"Screen {idx + 1}{main}  ·  {r - l}x{b - t}  ·  {dev.lstrip(chr(92) + '.')}",
                                  "type": "ddagrab", "index": idx})
                    covered.add(hmon)
            except Exception:
                pass
        try:
            for i, (x, y, w, h, hmon) in enumerate(windows_monitors()):
                if hmon not in covered:
                    video.append({"label": f"Screen at {x},{y}  ·  {w}x{h}  ·  compatibility (GDI)",
                                  "type": "gdigrab", "x": x, "y": y, "w": w, "h": h})
        except Exception:
            pass
        video.append({"label": "Entire desktop  ·  compatibility (GDI)", "type": "gdigrab"})
        if not has_dda:
            notes.append("This FFmpeg has no ddagrab (GPU screen capture, FFmpeg 6.0+). GDI capture "
                         "can fail when monitors use different display scaling. Updating FFmpeg is recommended.")
        v, a = parse_dshow(run_quiet([ff, "-hide_banner", "-list_devices", "true",
                                      "-f", "dshow", "-i", "dummy"]))
        video += [{"label": f"Camera: {n}", "type": "dshow_video", "name": n} for n in v]
        audio += [{"label": n, "type": "dshow_audio", "name": n} for n in a]
        notes.append("To send computer audio, enable 'Stereo Mix' in Sound settings or install "
                     "a loopback device such as VB-CABLE, then pick it here.")

    elif SYSTEM == "Darwin":
        v, a = parse_avfoundation(run_quiet([ff, "-hide_banner", "-f", "avfoundation",
                                             "-list_devices", "true", "-i", ""]))
        for idx, name in v:
            screen = name.lower().startswith("capture screen")
            video.append({"label": name if screen else f"Camera: {name}",
                          "type": "avf_video", "index": idx, "screen": screen})
        audio += [{"label": name, "type": "avf_audio", "index": idx} for idx, name in a]
        notes.append("macOS can't capture system audio by itself: install a loopback driver "
                     "such as BlackHole and pick it here.")
        notes.append("Grant Screen Recording and Microphone permission to the app running this.")

    else:  # Linux / BSD
        display = os.environ.get("DISPLAY", ":0")
        if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
            notes.append("Wayland session detected: x11grab only sees XWayland windows. "
                         "Log in with an X11/Xorg session for full-screen capture.")
        xr = run_quiet(["xrandr", "--listmonitors"]) if shutil.which("xrandr") else ""
        for line in xr.splitlines():
            m = re.search(r"(\d+)/\d+x(\d+)/\d+\+(-?\d+)\+(-?\d+)\s+(\S+)\s*$", line)
            if m:
                w, h, x, y, name = int(m[1]), int(m[2]), int(m[3]), int(m[4]), m[5]
                video.append({"label": f"Screen {name}  ({w}x{h} at {x},{y})", "type": "x11grab",
                              "display": display, "x": x, "y": y, "w": w, "h": h})
        video.append({"label": f"Entire display {display}", "type": "x11grab", "display": display})
        for dev in sorted(os.listdir("/dev")) if os.path.isdir("/dev") else []:
            if re.fullmatch(r"video\d+", dev):
                name = dev
                try:
                    with open(f"/sys/class/video4linux/{dev}/name") as f:
                        name = f.read().strip()
                except OSError:
                    pass
                video.append({"label": f"Camera: {name} (/dev/{dev})", "type": "v4l2",
                              "device": f"/dev/{dev}"})
        if shutil.which("pactl"):
            for line in run_quiet(["pactl", "list", "short", "sources"]).splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    name = parts[1]
                    tag = "  (computer audio)" if name.endswith(".monitor") else ""
                    audio.append({"label": name + tag, "type": "pulse", "name": name})
            if audio:
                audio.insert(0, {"label": "Default input (PulseAudio/PipeWire)",
                                 "type": "pulse", "name": "default"})
                notes.append("Sources marked '(computer audio)' carry what the computer is playing.")
        if not audio and shutil.which("arecord"):
            for line in run_quiet(["arecord", "-l"]).splitlines():
                m = re.match(r"card (\d+): .*?\[(.*?)\], device (\d+):", line)
                if m:
                    audio.append({"label": f"{m[2]} (hw:{m[1]},{m[3]})", "type": "alsa",
                                  "name": f"hw:{m[1]},{m[3]}"})
    return video, audio, notes


def check_ffmpeg(ff):
    out = run_quiet([ff, "-hide_banner", "-version"])
    if not out.strip() or "ffmpeg version" not in out:
        return {"ok": False}
    version = out.splitlines()[0].split(" Copyright")[0]
    protos = run_quiet([ff, "-hide_banner", "-protocols"])
    has_srt = bool(re.search(r"^\s*srt\s*$", protos, re.M))
    encs = set(re.findall(r"^\s*V[.\w]{5}\s+(\S+)", run_quiet([ff, "-hide_banner", "-encoders"]), re.M))
    return {"ok": True, "version": version, "srt": has_srt, "encoders": encs}


# ==========================================================================
# Command building
# ==========================================================================
def video_input_args(src, s):
    fps = str(s["fps"])
    mouse = "1" if s["draw_mouse"] else "0"
    t = src["type"]
    if t == "gdigrab":
        a = ["-f", "gdigrab", "-framerate", fps, "-draw_mouse", mouse]
        if "w" in src:
            a += ["-offset_x", str(src["x"]), "-offset_y", str(src["y"]),
                  "-video_size", f"{src['w']}x{src['h']}"]
        return a + ["-i", "desktop"]
    if t == "ddagrab":
        graph = f"ddagrab=output_idx={src['index']}:framerate={fps}:draw_mouse={mouse},hwdownload,format=bgra"
        return ["-f", "lavfi", "-i", graph]
    if t == "dshow_video":
        return ["-f", "dshow", "-rtbufsize", "256M", "-i", f"video={src['name']}"]
    if t == "avf_video":
        a = ["-f", "avfoundation", "-framerate", fps]
        if src.get("screen"):
            a += ["-capture_cursor", mouse]
        return a + ["-i", f"{src['index']}:none"]
    if t == "x11grab":
        a = ["-f", "x11grab", "-framerate", fps, "-draw_mouse", mouse]
        if "w" in src:
            return a + ["-video_size", f"{src['w']}x{src['h']}",
                        "-i", f"{src['display']}+{src['x']},{src['y']}"]
        return a + ["-i", src["display"]]
    if t == "v4l2":
        return ["-f", "v4l2", "-i", src["device"]]
    raise ValueError(f"unknown video source type {t}")


def audio_input_args(src):
    t = src["type"]
    if t == "dshow_audio":
        return ["-f", "dshow", "-audio_buffer_size", "50", "-i", f"audio={src['name']}"]
    if t == "avf_audio":
        return ["-f", "avfoundation", "-i", f"none:{src['index']}"]
    if t == "pulse":
        return ["-f", "pulse", "-i", src["name"]]
    if t == "alsa":
        return ["-f", "alsa", "-i", src["name"]]
    raise ValueError(f"unknown audio source type {t}")


def format_host(host):
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def build_srt_url(s):
    mode = s["mode"]
    host = s["host"].strip() or ("0.0.0.0" if mode == "listener" else "")
    latency_us = int(float(s["latency"]) * 1000)  # FFmpeg's libsrt latency is in microseconds
    return f"srt://{format_host(host)}:{int(s['port'])}?mode={mode}&latency={latency_us}&pkt_size=1316"


def preview_output_args(size, fps=6):
    w, h = size
    return ["-map", "0:v:0", "-an",
            "-vf", f"fps={fps},scale={w}:{h}:force_original_aspect_ratio=decrease:flags=bilinear,format=rgb24",
            "-c:v", "ppm", "-f", "image2pipe", "pipe:1"]


def build_command(s, video, audio, mask_secrets=False, stream=True, preview=None):
    """Build the FFmpeg argument list.

    stream=True   -> encode and send to SRT (plus an optional preview output)
    stream=False  -> preview only (frames to stdout, audio levels to the log)
    preview=(w,h) -> add a low-rate raw-frame output on stdout for the confidence monitor
    """
    enc = s["encoder"].split()[0]
    fps = int(float(s["fps"]))
    vb = int(float(s["vbitrate"]))
    cmd = [s["ffmpeg"] or "ffmpeg", "-hide_banner", "-loglevel", "info", "-stats"]
    cmd += ["-thread_queue_size", "1024"] + video_input_args(video, s)
    if audio:
        cmd += ["-thread_queue_size", "1024"] + audio_input_args(audio)

    if not stream:
        cmd += preview_output_args(preview or (640, 360))
        if audio:
            cmd += ["-map", "1:a:0", "-af", METER_FILTER, "-f", "null", "-"]
        return cmd

    cmd += ["-map", "0:v:0"] + (["-map", "1:a:0"] if audio else [])
    height = SCALES.get(s["scale"])
    vf = [f"scale=-2:{height}" if height else "scale=trunc(iw/2)*2:trunc(ih/2)*2",
          "format=nv12" if enc == "h264_qsv" else "format=yuv420p"]
    cmd += ["-vf", ",".join(vf), "-r", str(fps)]
    cmd += ["-c:v", enc] + ENCODER_ARGS.get(enc, [])
    cmd += ["-b:v", f"{vb}k", "-maxrate", f"{vb}k", "-bufsize", f"{vb * 2}k",
            "-g", str(int(fps * float(s["keyint"])))]
    if audio:
        af = "aresample=async=1000" + ("," + METER_FILTER if preview else "")
        cmd += ["-c:a", "aac", "-b:a", f"{s['abitrate']}k", "-ar", "48000", "-ac", "2", "-af", af]
    cmd += ["-f", "mpegts"]
    if s["passphrase"]:
        cmd += ["-passphrase", "********" if mask_secrets else s["passphrase"],
                "-pbkeylen", str(s["pbkeylen"])]
    if s["streamid"].strip():
        cmd += ["-streamid", s["streamid"].strip()]
    cmd.append(build_srt_url(s))
    if preview:
        cmd += preview_output_args(preview)
    return cmd


def local_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("192.0.2.1", 9))  # no packets are sent for a UDP connect
            return sk.getsockname()[0]
    except OSError:
        return "THIS-COMPUTER-IP"


def receiver_url(s, mask=True):
    """What the far end should use to receive this stream (FFmpeg/OBS URL syntax)."""
    try:
        port = int(s["port"])
        latency_us = int(float(s["latency"]) * 1000)
    except ValueError:
        return "(check port and latency)"
    mode = s["mode"]
    if mode == "caller":
        url = f"srt://0.0.0.0:{port}?mode=listener"
    elif mode == "listener":
        url = f"srt://{local_ip()}:{port}?mode=caller"
    else:
        url = f"srt://{local_ip()}:{port}?mode=rendezvous"
    url += f"&latency={latency_us}"
    if s["passphrase"]:
        url += f"&passphrase={'••••••••' if mask else s['passphrase']}&pbkeylen={s['pbkeylen']}"
    return url


def cmd_to_string(cmd):
    return subprocess.list2cmdline(cmd) if SYSTEM == "Windows" else shlex.join(cmd)


def validate(s, video):
    if not video:
        return "Pick a video source."
    try:
        port = int(s["port"])
        assert 1 <= port <= 65535
    except (ValueError, AssertionError):
        return "Port must be a number from 1 to 65535."
    if s["mode"] in ("caller", "rendezvous") and not s["host"].strip():
        return f"Enter the receiver's host or IP address ({s['mode']} mode)."
    for key, name in (("vbitrate", "Video bitrate"), ("latency", "Latency"), ("fps", "Frame rate")):
        try:
            assert float(s[key]) > 0
        except (ValueError, AssertionError):
            return f"{name} must be a positive number."
    if s["passphrase"] and not (10 <= len(s["passphrase"]) <= 79):
        return "The SRT passphrase must be 10-79 characters (or empty for no encryption)."
    return None


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    if not ffmpeg_exists(cfg["ffmpeg"]):
        cfg["ffmpeg"] = DEFAULTS["ffmpeg"]
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


def graceful_stop(proc, timeout=5):
    try:
        proc.stdin.write(b"q")
        proc.stdin.flush()
    except (OSError, ValueError):
        pass
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


# ==========================================================================
# Theme
# ==========================================================================
C = {
    "bg": "#0d0f13", "panel": "#14171e", "card": "#191d26", "card2": "#212633",
    "border": "#272d3a", "border_h": "#38405a", "input": "#0f1218",
    "text": "#e8eaf0", "muted": "#8b93a7", "faint": "#596175",
    "accent": "#3b82f6", "accent_h": "#5b97ff",
    "live": "#e5484d", "live_h": "#f0585d", "warn": "#f5a524", "ok": "#2fb170",
    "meter_g": "#2fb170", "meter_y": "#e2b93b", "meter_r": "#e5484d",
}
PILL = {  # state -> (label, background, foreground)
    "offline": ("OFFLINE", "#222734", "#8b93a7"),
    "preview": ("PREVIEW", "#14233f", "#79adff"),
    "connecting": ("CONNECTING", "#3a2b0d", "#f5a524"),
    "waiting": ("WAITING FOR RECEIVER", "#3a2b0d", "#f5a524"),
    "live": ("LIVE", "#3d1517", "#ff6166"),
    "reconnecting": ("RECONNECTING", "#3a2b0d", "#f5a524"),
    "error": ("ERROR", "#3d1517", "#ff6166"),
}
ICON_PNG = "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"


class Fonts:
    def __init__(self, root):
        fams = set(tkfont.families(root))
        prefs = {"Windows": ["Segoe UI Variable Text", "Segoe UI"],
                 "Darwin": ["SF Pro Text", ".AppleSystemUIFont", "Helvetica Neue"]}.get(
            SYSTEM, ["Inter", "Cantarell", "Ubuntu", "Noto Sans", "DejaVu Sans"])
        fam = next((f for f in prefs if f in fams), tkfont.nametofont("TkDefaultFont").actual("family"))
        mono = next((f for f in ("Cascadia Mono", "Consolas", "SF Mono", "Menlo", "JetBrains Mono",
                                 "DejaVu Sans Mono") if f in fams), "Courier")
        bump = 3 if SYSTEM == "Darwin" else 0

        def F(size, weight="normal", family=fam):
            return tkfont.Font(root, family=family, size=size + bump, weight=weight)

        self.base = F(10)
        self.bold = F(10, "bold")
        self.small = F(9)
        self.small_bold = F(9, "bold")
        self.label = F(8, "bold")
        self.title = F(13, "bold")
        self.stat = F(18, "bold")
        self.button = F(10, "bold")
        self.button_lg = F(12, "bold")
        self.mono = F(9, family=mono)
        self.placeholder = F(15, "bold")


def setup_style(root, fonts):
    st = ttk.Style(root)
    st.theme_use("clam")
    st.configure(".", background=C["panel"], foreground=C["text"], font=fonts.base,
                 bordercolor=C["border"], troughcolor=C["input"], focuscolor=C["panel"],
                 selectbackground=C["accent"], selectforeground="#ffffff",
                 insertcolor=C["text"], lightcolor=C["border"], darkcolor=C["border"])
    st.configure("TFrame", background=C["panel"])
    st.configure("TLabel", background=C["panel"], foreground=C["text"])
    st.configure("Field.TLabel", foreground=C["muted"], font=fonts.label)
    st.configure("Help.TLabel", foreground=C["muted"], font=fonts.small)
    st.configure("Link.TLabel", foreground=C["accent"], font=fonts.small_bold)
    field = dict(fieldbackground=C["input"], foreground=C["text"], bordercolor=C["border"],
                 lightcolor=C["input"], darkcolor=C["input"], insertcolor=C["text"],
                 padding=(9, 7), arrowcolor=C["muted"], background=C["card2"], arrowsize=12)
    for w in ("TEntry", "TCombobox", "TSpinbox"):
        st.configure(w, **field)
        st.map(w,
               bordercolor=[("focus", C["accent"]), ("hover", C["border_h"])],
               fieldbackground=[("disabled", C["panel"]), ("readonly", C["input"])],
               foreground=[("disabled", C["faint"])],
               selectbackground=[("readonly", C["input"])],
               selectforeground=[("readonly", C["text"])],
               arrowcolor=[("disabled", C["faint"]), ("hover", C["text"])],
               background=[("disabled", C["panel"]), ("active", C["border_h"])],
               lightcolor=[("focus", C["input"])], darkcolor=[("focus", C["input"])])
    st.configure("TCheckbutton", background=C["panel"], foreground=C["text"],
                 indicatorbackground=C["input"], indicatorforeground="#ffffff",
                 upperbordercolor=C["border_h"], lowerbordercolor=C["border_h"],
                 indicatormargin=(0, 0, 8, 0), padding=(0, 4))
    st.map("TCheckbutton",
           indicatorbackground=[("disabled", C["panel"]), ("selected", C["accent"])],
           foreground=[("disabled", C["faint"])], background=[("active", C["panel"])])
    st.configure("Vertical.TScrollbar", background=C["card2"], troughcolor=C["input"],
                 bordercolor=C["input"], arrowcolor=C["muted"], lightcolor=C["card2"],
                 darkcolor=C["card2"], gripcount=0)
    st.map("Vertical.TScrollbar", background=[("active", C["border_h"])])
    root.option_add("*TCombobox*Listbox.background", C["card"])
    root.option_add("*TCombobox*Listbox.foreground", C["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", C["accent"])
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
    root.option_add("*TCombobox*Listbox.font", fonts.base)
    root.option_add("*TCombobox*Listbox.borderWidth", 0)
    root.option_add("*TCombobox*Listbox.relief", "flat")


# ==========================================================================
# Custom widgets
# ==========================================================================
def round_rect(cv, x1, y1, x2, y2, r, **kw):
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


class RoundButton(tk.Canvas):
    def __init__(self, parent, text, command, font, kind="secondary", width=120, height=36,
                 parent_bg=None):
        super().__init__(parent, width=width, height=height, highlightthickness=0, bd=0,
                         bg=parent_bg or parent.cget("bg"), cursor="hand2")
        self.text, self.command, self.font, self.kind = text, command, font, kind
        self.enabled, self.hover = True, False
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", self._click)

    def _colors(self):
        if not self.enabled:
            return C["card"], C["faint"], C["border"]
        if self.kind == "primary":
            return (C["accent_h"] if self.hover else C["accent"]), "#ffffff", None
        if self.kind == "live":
            return (C["live_h"] if self.hover else C["live"]), "#ffffff", None
        if self.kind == "active":
            return "#1b2d4f", "#8ab8ff", C["accent"]
        return (C["card2"] if self.hover else C["card"]), C["text"], (C["border_h"] if self.hover else C["border"])

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        fill, fg, border = self._colors()
        round_rect(self, 1, 1, w - 1, h - 1, 8, fill=fill, outline=border or fill)
        self.create_text(w / 2, h / 2, text=self.text, fill=fg, font=self.font)

    def _set_hover(self, on):
        self.hover = on
        self._draw()

    def _click(self, e):
        if self.enabled and 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height():
            self.command()

    def update_button(self, text=None, kind=None, enabled=None):
        if text is not None:
            self.text = text
        if kind is not None:
            self.kind = kind
        if enabled is not None:
            self.enabled = enabled
            self.configure(cursor="hand2" if enabled else "arrow")
        self._draw()

    def set_enabled(self, on):
        self.update_button(enabled=on)


class Segmented(tk.Frame):
    """A segmented control bound to a StringVar."""

    def __init__(self, parent, variable, options, font):
        super().__init__(parent, bg=C["border"], padx=1, pady=1)
        self.var, self.enabled, self.labels = variable, True, {}
        for i, (value, text) in enumerate(options):
            lbl = tk.Label(self, text=text, font=font, padx=8, pady=7, cursor="hand2")
            lbl.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 1, 0))
            self.columnconfigure(i, weight=1, uniform="seg")
            lbl.bind("<Button-1>", lambda e, v=value: self.enabled and self.var.set(v))
            self.labels[value] = lbl
        variable.trace_add("write", lambda *_: self._render())
        self._render()

    def _render(self):
        cur = self.var.get()
        for v, lbl in self.labels.items():
            if v == cur:
                lbl.configure(bg=C["accent"] if self.enabled else C["card2"],
                              fg="#ffffff" if self.enabled else C["muted"])
            else:
                lbl.configure(bg=C["input"], fg=C["muted"] if self.enabled else C["faint"])

    def set_enabled(self, on):
        self.enabled = on
        for lbl in self.labels.values():
            lbl.configure(cursor="hand2" if on else "arrow")
        self._render()


class StatusPill(tk.Canvas):
    def __init__(self, parent, font):
        super().__init__(parent, height=28, width=120, highlightthickness=0, bd=0, bg=parent.cget("bg"))
        self.font, self.state, self.blink = font, "offline", True
        self.set("offline")

    def set(self, state):
        self.state = state
        self._draw()

    def toggle_blink(self):
        self.blink = not self.blink
        if self.state == "live":
            self._draw()

    def _draw(self):
        label, bg, fg = PILL[self.state]
        tw = self.font.measure(label)
        w = tw + 40
        self.configure(width=w)
        self.delete("all")
        round_rect(self, 1, 1, w - 1, 27, 13, fill=bg, outline=bg)
        dot = fg if (self.state != "live" or self.blink) else bg
        self.create_oval(13, 10, 21, 18, fill=dot, outline=dot)
        self.create_text(28, 14, text=label, anchor="w", fill=fg, font=self.font)


class TabBar(tk.Frame):
    def __init__(self, parent, tabs, font, command):
        super().__init__(parent, bg=C["panel"])
        self.items, self.command = {}, command
        for i, (key, text) in enumerate(tabs):
            cell = tk.Frame(self, bg=C["panel"], cursor="hand2")
            cell.grid(row=0, column=i, sticky="nsew")
            self.columnconfigure(i, weight=1, uniform="tab")
            lbl = tk.Label(cell, text=text, font=font, bg=C["panel"], pady=10, cursor="hand2")
            lbl.pack(fill="x")
            bar = tk.Frame(cell, height=2, bg=C["panel"])
            bar.pack(fill="x")
            for w in (cell, lbl):
                w.bind("<Button-1>", lambda e, k=key: self.select(k))
            self.items[key] = (lbl, bar)

    def select(self, key):
        for k, (lbl, bar) in self.items.items():
            lbl.configure(fg=C["text"] if k == key else C["muted"])
            bar.configure(bg=C["accent"] if k == key else C["panel"])
        self.command(key)


class Meter(tk.Canvas):
    """Stereo peak meter, -60..0 dBFS, with peak hold."""
    TICKS = (0, -6, -12, -18, -24, -36, -48, -60)

    def __init__(self, parent, font, width=64):
        super().__init__(parent, width=width, highlightthickness=0, bd=0, bg=C["bg"])
        self.font = font
        self.level = [-90.0, -90.0]
        self.hold = [-90.0, -90.0]
        self.hold_t = [0.0, 0.0]
        self.active = False
        self.bind("<Configure>", lambda e: self._draw_static())

    def _geom(self):
        w, h = self.winfo_width(), self.winfo_height()
        top, bot = 8, h - 24
        bw = 11
        x0 = w - 2 * bw - 6
        return top, bot, [(x0, x0 + bw), (x0 + bw + 4, x0 + 2 * bw + 4)]

    def _y(self, db):
        top, bot, _ = self._geom()
        frac = (max(-60.0, min(0.0, db)) + 60.0) / 60.0
        return bot - frac * (bot - top)

    def _draw_static(self):
        self.delete("all")
        top, bot, bars = self._geom()
        if bot - top < 40:
            return
        for x1, x2 in bars:
            self.create_rectangle(x1, top, x2, bot, fill=C["input"], outline=C["border"])
        for db in self.TICKS:
            y = self._y(db)
            self.create_text(bars[0][0] - 6, y, text=str(db), anchor="e", fill=C["faint"], font=self.font)
        for (x1, x2), ch in zip(bars, "LR"):
            self.create_text((x1 + x2) / 2, bot + 12, text=ch, fill=C["muted"], font=self.font)
        y = top + 2
        while y < bot:
            for x1, x2 in bars:
                self.create_line(x1 + 1, y, x2, y, fill=C["input"], tags="grid")
            y += 3
        self._draw_levels()

    def update_levels(self, peaks, dt):
        now = time.time()
        for i in range(2):
            p = peaks[i] if peaks else -90.0
            self.level[i] = p if p > self.level[i] else max(p, self.level[i] - 24.0 * dt)
            if self.level[i] >= self.hold[i] or now - self.hold_t[i] > 1.5:
                if self.level[i] >= self.hold[i]:
                    self.hold[i], self.hold_t[i] = self.level[i], now
                else:
                    self.hold[i] = max(self.level[i], self.hold[i] - 30.0 * dt)
        self._draw_levels()

    def _draw_levels(self):
        self.delete("lit")
        top, bot, bars = self._geom()
        if bot - top < 40:
            return
        zones = ((-60, -18, C["meter_g"]), (-18, -6, C["meter_y"]), (-6, 0, C["meter_r"]))
        for i, (x1, x2) in enumerate(bars):
            lvl = self.level[i]
            if lvl > -60:
                for lo, hi, col in zones:
                    if lvl > lo:
                        self.create_rectangle(x1 + 1, self._y(min(lvl, hi)), x2, self._y(lo),
                                              fill=col, outline="", tags="lit")
            if self.hold[i] > -59:
                y = self._y(self.hold[i])
                col = C["meter_r"] if self.hold[i] > -6 else C["text"]
                self.create_line(x1 + 1, y, x2, y, fill=col, width=2, tags="lit")
        self.tag_raise("grid")


class Sparkline(tk.Canvas):
    def __init__(self, parent, height=46, bg=None):
        super().__init__(parent, height=height, highlightthickness=0, bd=0, bg=bg or C["card"])
        self.values, self.ceiling = [], 1.0
        self.bind("<Configure>", lambda e: self._draw())

    def set(self, values, ceiling):
        self.values, self.ceiling = list(values), max(ceiling, 1.0)
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        n = 90
        for frac in (0.5,):
            self.create_line(0, h * frac, w, h * frac, fill=C["border"], dash=(2, 4))
        if len(self.values) < 2:
            return
        vals = self.values[-n:]
        step = w / (n - 1)
        x0 = w - step * (len(vals) - 1)
        pts = []
        for i, v in enumerate(vals):
            pts += [x0 + i * step, h - 2 - (min(v, self.ceiling) / self.ceiling) * (h - 6)]
        self.create_polygon([x0, h] + pts + [pts[-2], h], fill="#16284a", outline="")
        self.create_line(pts, fill=C["accent"], width=2, smooth=True)


# ==========================================================================
# Application
# ==========================================================================
STATS_RE = re.compile(r"frame=\s*(\d+).*?fps=\s*([\d.]+).*?bitrate=\s*(\S+).*?speed=\s*(\S+)")
DROP_RE = re.compile(r"drop=\s*(\d+)")
METER_RE = re.compile(r"lavfi\.astats\.(\d+)\.Peak_level=(-?inf|-?[\d.]+)")
ERROR_RE = re.compile(r"error|fail|invalid|not found|denied|refused|cannot|could not|unable|"
                      r"timed out|timeout|no such|broken|reject|permission", re.I)
SOURCE_KEYS = ("video", "audio", "draw_mouse", "fps", "ffmpeg")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.withdraw()
        self.title(APP_NAME)
        self.k = max(1.0, self.winfo_fpixels("1i") / 96.0)  # UI scale for pixel sizes
        self.geometry(f"{self.S(1280)}x{self.S(820)}")
        self.minsize(self.S(1120), self.S(720))
        self.configure(bg=C["bg"])
        self.fonts = Fonts(self)
        setup_style(self, self.fonts)
        try:
            self.icon = tk.PhotoImage(data=ICON_PNG)
            self.iconphoto(True, self.icon)
        except tk.TclError:
            self.icon = None

        self.cfg = load_config()
        self.video_map, self.audio_map = {}, {"None": None}
        self.q = queue.Queue()
        self.proc = None
        self.proc_kind = None
        self.gen = 0
        self.pending = None
        self.want_stream = False
        self.want_preview = False
        self.live = False
        self.retry_count = 0
        self.retry_job = None
        self.restart_job = None
        self.started_at = None
        self.recent_lines = collections.deque(maxlen=40)
        self.bitrate_hist = collections.deque(maxlen=120)
        self._frame = None
        self._shown_frame = None
        self._photo = None
        self._meter_acc = {}
        self._last_tick = time.time()
        self._last_blink = 0.0
        self.log_open = False

        self._build_vars()
        self._build_ui()
        self._on_settings_changed()
        self._update_controls()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind_all("<Control-Return>", lambda e: self.toggle_live())
        self.deiconify()
        self._dark_titlebar()
        self.after(40, self._tick)
        self.refresh_devices()
        self.check_ffmpeg(show=False)

    def S(self, px):
        return int(round(px * self.k))

    # ------------------------------------------------------------------ vars
    def _build_vars(self):
        self.vars = {}
        for key, val in self.cfg.items():
            var = tk.BooleanVar(value=bool(val)) if isinstance(DEFAULTS[key], bool) else tk.StringVar(value=str(val))
            var.trace_add("write", lambda *_a, k=key: self._on_settings_changed(k))
            self.vars[key] = var
        self.mode_hint = tk.StringVar()
        self.ff_status = tk.StringVar(value="Checking FFmpeg…")
        self.notes = tk.StringVar(value="Scanning devices…")
        self.msg = tk.StringVar(value="Ready. Pick your sources, enter the SRT destination, then Go Live.")
        self.receiver = tk.StringVar()
        self.stat_vars = {k: tk.StringVar(value="—") for k in
                          ("bitrate", "bitrate_sub", "fps", "fps_sub", "drop", "drop_sub", "uptime", "uptime_sub")}
        self.show_pass = tk.BooleanVar(value=False)

    def settings(self):
        return {k: v.get() for k, v in self.vars.items()}

    def selected_sources(self):
        return self.video_map.get(self.vars["video"].get()), self.audio_map.get(self.vars["audio"].get())

    # ---------------------------------------------------------------- layout
    def _build_ui(self):
        S, F = self.S, self.fonts
        # Header
        header = tk.Frame(self, bg=C["panel"], height=S(60))
        header.pack(side="top", fill="x")
        header.pack_propagate(False)
        brand = tk.Frame(header, bg=C["panel"])
        brand.pack(side="left", padx=S(18))
        if self.icon:
            self.icon_small = self.icon.subsample(2)
            tk.Label(brand, image=self.icon_small, bg=C["panel"]).pack(side="left", pady=S(12))
        names = tk.Frame(brand, bg=C["panel"])
        names.pack(side="left", padx=(S(10), 0))
        tk.Label(names, text=APP_NAME, font=F.title, bg=C["panel"], fg=C["text"]).pack(anchor="w")
        tk.Label(names, text="Screen capture to SRT, powered by FFmpeg", font=F.small,
                 bg=C["panel"], fg=C["muted"]).pack(anchor="w")
        right = tk.Frame(header, bg=C["panel"])
        right.pack(side="right", padx=S(18))
        self.uptime_hdr = tk.Label(right, text="", font=F.mono, bg=C["panel"], fg=C["muted"])
        self.uptime_hdr.pack(side="right", padx=(S(12), 0))
        self.pill = StatusPill(right, F.small_bold)
        self.pill.pack(side="right", pady=S(16))
        tk.Frame(self, bg=C["border"], height=1).pack(fill="x")

        body = tk.Frame(self, bg=C["bg"])
        body.pack(fill="both", expand=True)
        sidebar = tk.Frame(body, bg=C["panel"], width=S(390))
        sidebar.pack(side="right", fill="y")
        sidebar.pack_propagate(False)
        tk.Frame(body, bg=C["border"], width=1).pack(side="right", fill="y")
        main = tk.Frame(body, bg=C["bg"], padx=S(20), pady=S(18))
        main.pack(side="left", fill="both", expand=True)
        self._build_main(main)
        self._build_sidebar(sidebar)

    def _card(self, parent, **grid):
        f = tk.Frame(parent, bg=C["card"], highlightthickness=1, highlightbackground=C["border"],
                     padx=self.S(14), pady=self.S(12))
        f.grid(**grid)
        return f

    def _build_main(self, main):
        S, F = self.S, self.fonts
        main.columnconfigure(0, weight=1)
        main.rowconfigure(0, weight=1)

        # Preview + meters
        pv = tk.Frame(main, bg=C["bg"])
        pv.grid(row=0, column=0, sticky="nsew")
        self.canvas = tk.Canvas(pv, bg="#06070a", highlightthickness=1, highlightbackground=C["border"])
        self.canvas.pack(side="left", fill="both", expand=True)
        self.meter = Meter(pv, F.small, width=S(62))
        self.meter.pack(side="right", fill="y", padx=(S(8), 0))
        self.img_item = self.canvas.create_image(0, 0, anchor="center", state="hidden")
        self.ph_title = self.canvas.create_text(0, 0, text="Preview is off", fill=C["muted"], font=F.placeholder)
        self.ph_sub = self.canvas.create_text(0, 0, fill=C["faint"], font=F.base, justify="center",
                                              text="Click Preview to check your source before going live.")
        self.badge = self.canvas.create_text(0, 0, anchor="nw", text="", fill="#ffffff", font=F.small_bold)
        self.badge_bg = self.canvas.create_rectangle(0, 0, 0, 0, fill="", outline="")
        self.canvas.bind("<Configure>", lambda e: self._layout_canvas())

        # Controls
        ctl = tk.Frame(main, bg=C["bg"])
        ctl.grid(row=1, column=0, sticky="ew", pady=(S(14), S(14)))
        ctl.columnconfigure(1, weight=1)
        self.preview_btn = RoundButton(ctl, "Preview", self.toggle_preview, F.button,
                                       width=S(124), height=S(42), parent_bg=C["bg"])
        self.preview_btn.grid(row=0, column=0, sticky="w")
        self.msg_lbl = tk.Label(ctl, textvariable=self.msg, font=F.small, bg=C["bg"], fg=C["muted"],
                                anchor="w", justify="left", wraplength=S(520))
        self.msg_lbl.grid(row=0, column=1, sticky="ew", padx=S(16))
        self.live_btn = RoundButton(ctl, "Go Live", self.toggle_live, F.button_lg, kind="live",
                                    width=S(172), height=S(42), parent_bg=C["bg"])
        self.live_btn.grid(row=0, column=2, sticky="e")

        # Stats cards
        stats = tk.Frame(main, bg=C["bg"])
        stats.grid(row=2, column=0, sticky="ew")
        for i, w in enumerate((3, 2, 2, 2)):
            stats.columnconfigure(i, weight=w, minsize=S(150 if i else 260))
        cards = [("BITRATE", "bitrate"), ("FRAME RATE", "fps"), ("DROPPED FRAMES", "drop"), ("UPTIME", "uptime")]
        for i, (title, key) in enumerate(cards):
            card = self._card(stats, row=0, column=i, sticky="nsew", padx=(0 if i == 0 else S(10), 0))
            if key == "bitrate":
                card.columnconfigure(1, weight=1)
                txt = tk.Frame(card, bg=C["card"])
                txt.grid(row=0, column=0, sticky="nw")
                self.spark = Sparkline(card, height=S(50))
                self.spark.grid(row=0, column=1, sticky="nsew", padx=(S(14), 0))
                target = txt
            else:
                target = card
            tk.Label(target, text=title, font=F.label, bg=C["card"], fg=C["muted"]).pack(anchor="w")
            tk.Label(target, textvariable=self.stat_vars[key], font=F.stat, bg=C["card"],
                     fg=C["text"]).pack(anchor="w", pady=(S(2), 0))
            tk.Label(target, textvariable=self.stat_vars[key + "_sub"], font=F.small, bg=C["card"],
                     fg=C["muted"]).pack(anchor="w")

        # Log drawer
        drawer = tk.Frame(main, bg=C["card"], highlightthickness=1, highlightbackground=C["border"])
        drawer.grid(row=3, column=0, sticky="ew", pady=(S(12), 0))
        drawer.columnconfigure(0, weight=1)
        head = tk.Frame(drawer, bg=C["card"], padx=S(12), pady=S(8))
        head.grid(row=0, column=0, sticky="ew")
        self.log_toggle = tk.Label(head, text="▸  Log", font=F.small_bold, bg=C["card"], fg=C["text"], cursor="hand2")
        self.log_toggle.pack(side="left")
        self.log_toggle.bind("<Button-1>", lambda e: self._toggle_log())
        for text, cmd in (("Clear", self._clear_log), ("Copy command", self._copy_command)):
            lbl = tk.Label(head, text=text, font=F.small_bold, bg=C["card"], fg=C["accent"], cursor="hand2")
            lbl.pack(side="right", padx=(S(14), 0))
            lbl.bind("<Button-1>", lambda e, c=cmd: c())
        self.log_body = tk.Frame(drawer, bg=C["card"])
        self.log_body.columnconfigure(0, weight=1)
        self.log = tk.Text(self.log_body, height=9, font=F.mono, bg=C["input"], fg="#b9c0d0",
                           insertbackground=C["text"], relief="flat", padx=S(10), pady=S(8),
                           highlightthickness=0, state="disabled", wrap="word")
        self.log.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(self.log_body, orient="vertical", command=self.log.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=sb.set)
        self.log.tag_configure("cmd", foreground="#79adff")
        self.log.tag_configure("err", foreground="#ff8589")
        self.log.tag_configure("app", foreground=C["ok"])

    def _build_sidebar(self, side):
        S, F = self.S, self.fonts
        self.lock_banner = tk.Label(side, text="Settings are locked while you're live.",
                                    font=F.small_bold, bg="#3d1517", fg="#ff8f93", pady=S(8))
        footer = tk.Frame(side, bg=C["panel"], padx=S(20), pady=S(12))
        footer.pack(side="bottom", fill="x")
        tk.Frame(side, bg=C["border"], height=1).pack(side="bottom", fill="x")
        self.ff_lbl = tk.Label(footer, textvariable=self.ff_status, font=F.small, bg=C["panel"],
                               fg=C["muted"], anchor="w", justify="left", wraplength=S(350))
        self.ff_lbl.pack(fill="x")

        tabs = [("sources", "Sources"), ("encoding", "Encoding"), ("srt", "SRT"), ("advanced", "Advanced")]
        self.tabbar = TabBar(side, tabs, F.bold, self._show_tab)
        self.tabbar.pack(side="top", fill="x", padx=S(12), pady=(S(6), 0))
        tk.Frame(side, bg=C["border"], height=1).pack(side="top", fill="x")
        self.lock_anchor = tk.Frame(side, bg=C["panel"], height=0)
        self.lock_anchor.pack(side="top", fill="x")
        holder = tk.Frame(side, bg=C["panel"], padx=S(20), pady=S(8))
        holder.pack(side="top", fill="both", expand=True)
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)

        self.lockables = []
        self.tabs = {}
        for key, _ in tabs:
            f = ttk.Frame(holder)
            f.grid(row=0, column=0, sticky="nsew")
            f.columnconfigure(0, weight=1, uniform="col")
            f.columnconfigure(1, weight=1, uniform="col")
            self.tabs[key] = f
        self._build_tab_sources(self.tabs["sources"])
        self._build_tab_encoding(self.tabs["encoding"])
        self._build_tab_srt(self.tabs["srt"])
        self._build_tab_advanced(self.tabs["advanced"])
        self.widget_states = {w: str(w.cget("state")) for w in self.lockables if isinstance(w, ttk.Widget)}
        self.tabbar.select("sources")

    def _show_tab(self, key):
        self.tabs[key].tkraise()

    # form helpers
    def _pad(self, col, span):
        g = self.S(6)
        return 0 if span == 2 else ((0, g) if col == 0 else (g, 0))

    def _field(self, parent, row, label, widget, col=0, span=2, link=None):
        head = ttk.Frame(parent)
        head.grid(row=row, column=col, columnspan=span, sticky="ew",
                  pady=(self.S(14), self.S(5)), padx=self._pad(col, span))
        ttk.Label(head, text=label.upper(), style="Field.TLabel").pack(side="left")
        if link:
            text, cmd = link
            lk = ttk.Label(head, text=text, style="Link.TLabel", cursor="hand2")
            lk.pack(side="right")
            lk.bind("<Button-1>", lambda e: cmd())
        widget.grid(row=row + 1, column=col, columnspan=span, sticky="ew", padx=self._pad(col, span))
        if isinstance(widget, (ttk.Entry, ttk.Combobox, ttk.Spinbox, RoundButton, Segmented)):
            self.lockables.append(widget)
        return row + 2

    def _help(self, parent, row, text=None, var=None, col=0, span=2):
        lbl = ttk.Label(parent, style="Help.TLabel", wraplength=self.S(340 if span == 2 else 160),
                        justify="left", **({"textvariable": var} if var else {"text": text}))
        lbl.grid(row=row, column=col, columnspan=span, sticky="w", pady=(self.S(6), 0), padx=self._pad(col, span))
        return row + 1

    def _combo(self, parent, key, values=(), readonly=True, width=10):
        return ttk.Combobox(parent, textvariable=self.vars[key], values=list(values),
                            state="readonly" if readonly else "normal", width=width, font=self.fonts.base)

    def _spin(self, parent, key, lo, hi, inc=1):
        return ttk.Spinbox(parent, textvariable=self.vars[key], from_=lo, to=hi, increment=inc,
                           width=8, font=self.fonts.base)

    def _check(self, parent, row, key, text):
        cb = ttk.Checkbutton(parent, text=text, variable=self.vars[key])
        cb.grid(row=row, column=0, columnspan=2, sticky="w", pady=(self.S(10), 0))
        self.lockables.append(cb)
        return row + 1

    def _build_tab_sources(self, t):
        self.video_cb = self._combo(t, "video")
        r = self._field(t, 0, "Video source", self.video_cb, link=("↻  Refresh devices", self.refresh_devices))
        r = self._check(t, r, "draw_mouse", "Show mouse cursor")
        self.audio_cb = self._combo(t, "audio")
        r = self._field(t, r, "Audio source", self.audio_cb)
        r = self._help(t, r, var=self.notes)

    def _build_tab_encoding(self, t):
        S, F = self.S, self.fonts
        chips = ttk.Frame(t)
        for i, (label, values) in enumerate(PRESETS):
            b = RoundButton(chips, label, lambda v=values: self._apply_preset(v), F.small_bold,
                            width=S(160), height=S(32), parent_bg=C["panel"])
            b.grid(row=i // 2, column=i % 2, sticky="ew", padx=(0 if i % 2 == 0 else S(6), 0), pady=(0, S(6)))
            chips.columnconfigure(i % 2, weight=1, uniform="chip")
            self.lockables.append(b)
        r = self._field(t, 0, "Quick presets", chips)
        self.enc_cb = self._combo(t, "encoder", ENCODERS)
        r = self._field(t, r, "Video encoder", self.enc_cb)
        self._field(t, r, "Video bitrate (kbps)", self._spin(t, "vbitrate", 300, 50000, 250), col=0, span=1)
        r = self._field(t, r, "Frame rate", self._combo(t, "fps", ("24", "25", "30", "50", "60"), readonly=False), col=1, span=1)
        self._field(t, r, "Resolution", self._combo(t, "scale", SCALES), col=0, span=1)
        r = self._field(t, r, "Keyframe every (s)", self._combo(t, "keyint", ("0.5", "1", "2", "4"), readonly=False), col=1, span=1)
        r = self._field(t, r, "Audio bitrate (kbps)", self._combo(t, "abitrate", ("96", "128", "160", "192", "256", "320"), readonly=False), col=0, span=1)
        r = self._help(t, r, "x264 runs on the CPU. NVENC, Quick Sync and AMF use your graphics "
                             "hardware and leave the CPU free. Only encoders your FFmpeg build has are listed.")

    def _build_tab_srt(self, t):
        S, F = self.S, self.fonts
        seg = Segmented(t, self.vars["mode"], [("caller", "Caller"), ("listener", "Listener"),
                                               ("rendezvous", "Rendezvous")], F.small_bold)
        r = self._field(t, 0, "Connection mode", seg)
        r = self._help(t, r, var=self.mode_hint)
        host_port = ttk.Frame(t)
        host_port.columnconfigure(0, weight=1)
        host = ttk.Entry(host_port, textvariable=self.vars["host"], font=F.base)
        host.grid(row=0, column=0, sticky="ew")
        port = self._spin(host_port, "port", 1, 65535)
        port.configure(width=7)
        port.grid(row=0, column=1, padx=(S(8), 0))
        self.lockables += [host, port]
        r = self._field(t, r, "Host / IP  ·  Port", host_port)
        r = self._field(t, r, "Latency (ms)", self._spin(t, "latency", 20, 8000, 20), col=0, span=1)
        self._help(t, r - 1, "≈ 4× round-trip time: 120 ms on a LAN, 500–2000 ms over the internet.", col=1, span=1)

        pw = ttk.Frame(t)
        pw.columnconfigure(0, weight=1)
        self.pass_entry = ttk.Entry(pw, textvariable=self.vars["passphrase"], show="•", font=F.base)
        self.pass_entry.grid(row=0, column=0, sticky="ew")
        eye = ttk.Label(pw, text="Show", style="Link.TLabel", cursor="hand2")
        eye.grid(row=0, column=1, padx=(S(10), 0))

        def toggle_eye(_e=None):
            self.show_pass.set(not self.show_pass.get())
            self.pass_entry.configure(show="" if self.show_pass.get() else "•")
            eye.configure(text="Hide" if self.show_pass.get() else "Show")
        eye.bind("<Button-1>", toggle_eye)
        self.lockables.append(self.pass_entry)
        r = self._field(t, r, "Encryption passphrase (optional)", pw)
        keyseg = Segmented(t, self.vars["pbkeylen"], [("16", "AES-128"), ("24", "AES-192"), ("32", "AES-256")], F.small_bold)
        r = self._field(t, r, "Key length", keyseg)
        sid = ttk.Entry(t, textvariable=self.vars["streamid"], font=F.base)
        r = self._field(t, r, "Stream ID (optional)", sid)

        box = tk.Frame(t, bg=C["card"], highlightthickness=1, highlightbackground=C["border"],
                       padx=S(12), pady=S(10))
        box.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(S(18), 0))
        box.columnconfigure(0, weight=1)
        tk.Label(box, text="RECEIVER SETTINGS", font=F.label, bg=C["card"], fg=C["muted"]).grid(row=0, column=0, sticky="w")
        cp = tk.Label(box, text="Copy", font=F.small_bold, bg=C["card"], fg=C["accent"], cursor="hand2")
        cp.grid(row=0, column=1, sticky="e")
        cp.bind("<Button-1>", lambda e: self._copy_text(receiver_url(self.settings(), mask=False), "Receiver URL copied."))
        tk.Label(box, textvariable=self.receiver, font=F.mono, bg=C["card"], fg=C["text"], anchor="w",
                 justify="left", wraplength=S(320)).grid(row=1, column=0, columnspan=2, sticky="w", pady=(S(6), 0))
        tk.Label(box, text="Use this in ffplay, OBS (Media Source) or another FFmpeg-based receiver.",
                 font=F.small, bg=C["card"], fg=C["muted"], wraplength=S(320), justify="left").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(S(4), 0))

    def _build_tab_advanced(self, t):
        S, F = self.S, self.fonts
        ffrow = ttk.Frame(t)
        ffrow.columnconfigure(0, weight=1)
        ffe = ttk.Entry(ffrow, textvariable=self.vars["ffmpeg"], font=F.base)
        ffe.grid(row=0, column=0, sticky="ew")
        browse = RoundButton(ffrow, "Browse…", self._browse_ffmpeg, F.small_bold, width=S(84), height=S(34), parent_bg=C["panel"])
        browse.grid(row=0, column=1, padx=(S(8), 0))
        check = RoundButton(ffrow, "Check", lambda: self.check_ffmpeg(show=True), F.small_bold, width=S(70), height=S(34), parent_bg=C["panel"])
        check.grid(row=0, column=2, padx=(S(6), 0))
        self.lockables += [ffe, browse, check]
        r = self._field(t, 0, "FFmpeg location", ffrow)
        r = self._check(t, r, "reconnect", "Reconnect automatically if the stream drops")
        r = self._help(t, r, "Retries with a growing delay (2 s, 4 s, 8 s … up to 30 s) until you end the stream.")

        cmdbox = ttk.Frame(t)
        cmdbox.columnconfigure(0, weight=1)
        self.cmd_text = tk.Text(cmdbox, height=11, font=F.mono, bg=C["input"], fg="#b9c0d0", relief="flat",
                                padx=S(10), pady=S(8), highlightthickness=1, highlightbackground=C["border"],
                                wrap="word", state="disabled")
        self.cmd_text.grid(row=0, column=0, sticky="ew")
        r = self._field(t, r, "FFmpeg command", cmdbox, link=("Copy", self._copy_command))
        r = self._help(t, r, f"Settings are saved to {CONFIG_PATH} (the passphrase is stored in plain text).")

    # -------------------------------------------------------------- helpers
    def _dark_titlebar(self):
        if SYSTEM != "Windows":
            return
        try:
            import ctypes
            self.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            val = ctypes.c_int(1)
            for attr in (20, 19):
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                    break
        except Exception:
            pass

    def _layout_canvas(self):
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        self.canvas.coords(self.img_item, w / 2, h / 2)
        self.canvas.coords(self.ph_title, w / 2, h / 2 - self.S(14))
        self.canvas.coords(self.ph_sub, w / 2, h / 2 + self.S(16))
        self._draw_badge()

    def _draw_badge(self):
        text, color = "", ""
        if self.live:
            text, color = "  ● LIVE  ", C["live"]
        elif self.proc_kind == "preview":
            text, color = "  PREVIEW  ", C["accent"]
        self.canvas.itemconfigure(self.badge, text=text)
        if text:
            x, y = self.S(14), self.S(14)
            self.canvas.coords(self.badge, x, y + self.S(4))
            bx = self.canvas.bbox(self.badge)
            self.canvas.coords(self.badge_bg, bx[0] - 2, bx[1] - self.S(4), bx[2] + 2, bx[3] + self.S(4))
            self.canvas.itemconfigure(self.badge_bg, fill=color, outline=color)
            self.canvas.tag_raise(self.badge_bg)
            self.canvas.tag_raise(self.badge)
        else:
            self.canvas.itemconfigure(self.badge_bg, fill="", outline="")

    def _placeholder(self, title, sub):
        self.canvas.itemconfigure(self.img_item, image="", state="hidden")
        self.canvas.itemconfigure(self.ph_title, text=title, state="normal")
        self.canvas.itemconfigure(self.ph_sub, text=sub, state="normal")
        self._photo = None

    def _preview_size(self):
        w = max(160, min(self.canvas.winfo_width() - 4, 1280))
        h = max(90, min(self.canvas.winfo_height() - 4, 720))
        return w - w % 2, h - h % 2

    def _set_msg(self, text, kind="info"):
        self.msg.set(text)
        self.msg_lbl.configure(fg={"info": C["muted"], "error": "#ff8589", "ok": C["ok"], "warn": C["warn"]}[kind])

    def _write_log(self, text, tag=None):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag or ())
        if int(self.log.index("end-1c").split(".")[0]) > 3000:
            self.log.delete("1.0", "1000.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _toggle_log(self, open_=None):
        self.log_open = (not self.log_open) if open_ is None else open_
        if self.log_open:
            self.log_body.grid(row=1, column=0, sticky="ew")
        else:
            self.log_body.grid_remove()
        self.log_toggle.configure(text=("▾  Log" if self.log_open else "▸  Log"))

    def _copy_text(self, text, note):
        self.clipboard_clear()
        self.clipboard_append(text)
        self._set_msg(note, "ok")

    def _copy_command(self):
        video, audio = self.selected_sources()
        if video:
            try:
                self._copy_text(cmd_to_string(build_command(self.settings(), video, audio)), "FFmpeg command copied.")
            except (ValueError, KeyError):
                pass

    def _apply_preset(self, values):
        for k, v in values.items():
            self.vars[k].set(v)
        self._set_msg(f"Preset applied: {values['scale']} · {values['fps']} fps · {int(values['vbitrate']) / 1000:g} Mbps.", "ok")

    def _browse_ffmpeg(self):
        types = [("ffmpeg", "ffmpeg.exe"), ("All files", "*.*")] if SYSTEM == "Windows" else [("All files", "*")]
        path = filedialog.askopenfilename(title="Locate ffmpeg", filetypes=types)
        if path:
            self.vars["ffmpeg"].set(path)
            self.check_ffmpeg(show=True)
            self.refresh_devices()

    def _on_settings_changed(self, key=None):
        if not hasattr(self, "cmd_text"):
            return
        s = self.settings()
        self.mode_hint.set(MODE_HINTS.get(s["mode"], ""))
        self.receiver.set(receiver_url(s))
        video, audio = self.selected_sources()
        try:
            text = cmd_to_string(build_command(s, video, audio, mask_secrets=True)) if video else "(pick a video source)"
        except (ValueError, KeyError) as e:
            text = f"(incomplete settings: {e})"
        self.cmd_text.configure(state="normal")
        self.cmd_text.delete("1.0", "end")
        self.cmd_text.insert("1.0", text)
        self.cmd_text.configure(state="disabled")
        if key in SOURCE_KEYS and self.want_preview and not self.want_stream:
            if self.restart_job:
                self.after_cancel(self.restart_job)
            self.restart_job = self.after(400, self._restart_preview)

    def _restart_preview(self):
        self.restart_job = None
        if self.want_preview and not self.want_stream:
            self._stop_proc(then=self._sync)

    def _update_controls(self):
        streaming = self.want_stream
        self.live_btn.update_button(text="End Stream" if streaming else "Go Live",
                                    kind="secondary" if streaming else "live")
        self.preview_btn.update_button(text="Preview on" if (self.want_preview or streaming) else "Preview",
                                       kind="active" if (self.want_preview or streaming) else "secondary",
                                       enabled=not streaming)
        for w in self.lockables:
            if isinstance(w, (RoundButton, Segmented)):
                w.set_enabled(not streaming)
            else:
                try:
                    w.configure(state="disabled" if streaming else self.widget_states.get(w, "normal"))
                except tk.TclError:
                    pass
        if streaming:
            self.lock_banner.pack(after=self.lock_anchor, fill="x")
        else:
            self.lock_banner.pack_forget()
        self._draw_badge()

    # ------------------------------------------------------ background work
    def refresh_devices(self):
        self.notes.set("Scanning devices…")
        ff = self.vars["ffmpeg"].get() or "ffmpeg"
        threading.Thread(target=lambda: self.q.put(("devices", enumerate_sources(ff))), daemon=True).start()

    def check_ffmpeg(self, show):
        ff = self.vars["ffmpeg"].get() or "ffmpeg"
        threading.Thread(target=lambda: self.q.put(("ffcheck", (check_ffmpeg(ff), show))), daemon=True).start()

    def _apply_devices(self, video, audio, notes):
        self.video_map = {v["label"]: v for v in video}
        self.audio_map = {"None": None}
        self.audio_map.update({a["label"]: a for a in audio})
        self.video_cb.configure(values=list(self.video_map))
        self.audio_cb.configure(values=list(self.audio_map))
        if self.vars["video"].get() not in self.video_map:
            self.vars["video"].set(next(iter(self.video_map), ""))
        if self.vars["audio"].get() not in self.audio_map:
            self.vars["audio"].set("None")
        found = f"Found {len(video)} video and {len(audio)} audio source(s)."
        self.notes.set("\n\n".join([found] + notes))
        self._on_settings_changed()

    def _apply_ffcheck(self, info, show):
        if not info["ok"]:
            self.ff_status.set("✕  FFmpeg not found. Set its location in Advanced.")
            self.ff_lbl.configure(fg="#ff8589")
            if show:
                messagebox.showerror(APP_NAME, "Could not run FFmpeg at that location.")
            return
        version = info["version"].replace("ffmpeg version ", "FFmpeg ")
        if info["srt"]:
            self.ff_status.set(f"✓  {version}  ·  SRT supported")
            self.ff_lbl.configure(fg=C["muted"])
        else:
            self.ff_status.set(f"✕  {version} has no SRT support. Use a build with libsrt.")
            self.ff_lbl.configure(fg="#ff8589")
        avail = [e for e in ENCODERS if e.split()[0] in info["encoders"]] or ENCODERS
        self.enc_cb.configure(values=avail)
        if self.vars["encoder"].get() not in avail:
            self.vars["encoder"].set(avail[0])
        if show:
            if info["srt"]:
                self._set_msg(f"{version} is ready, with SRT and {len(avail)} usable video encoder(s).", "ok")
            else:
                messagebox.showwarning(APP_NAME, "This FFmpeg build has no SRT support (libsrt). "
                                                 "Get one that includes it, e.g. the gyan.dev builds on Windows "
                                                 "or Homebrew on macOS.")

    # ------------------------------------------------------ process control
    def toggle_live(self):
        if self.want_stream:
            if self.live and not messagebox.askyesno(APP_NAME, "End the stream?"):
                return
            self.want_stream = False
            self._cancel_retry()
            self._set_msg("Stream ended.")
            self._stop_proc(then=self._sync)
        else:
            s = self.settings()
            video, _ = self.selected_sources()
            err = validate(s, video)
            if err:
                self._set_msg(err, "error")
                if "host" in err.lower() or "port" in err.lower() or "passphrase" in err.lower():
                    self.tabbar.select("srt")
                return
            save_config(s)
            self.want_stream = True
            self.retry_count = 0
            self.bitrate_hist.clear()
            self._stop_proc(then=self._sync)
        self._update_controls()

    def toggle_preview(self):
        if self.want_stream:
            return
        self.want_preview = not self.want_preview
        if self.want_preview:
            video, _ = self.selected_sources()
            if not video:
                self.want_preview = False
                self._set_msg("Pick a video source first.", "error")
            else:
                self._set_msg("Previewing your source. Nothing is being sent yet.")
                self._stop_proc(then=self._sync)
        else:
            self._set_msg("Preview off.")
            self._stop_proc(then=self._sync)
        self._update_controls()

    def _cancel_retry(self):
        if self.retry_job:
            self.after_cancel(self.retry_job)
            self.retry_job = None

    def _stop_proc(self, then=None):
        if self.proc:
            self.pending = then
            threading.Thread(target=graceful_stop, args=(self.proc,), daemon=True).start()
        elif then:
            then()

    def _sync(self):
        """Start whatever should be running (stream > preview > nothing)."""
        self.retry_job = None
        if self.proc:
            return
        if self.want_stream:
            if self._spawn("stream"):
                listener = self.vars["mode"].get() == "listener"
                self.pill.set("waiting" if listener else ("reconnecting" if self.retry_count else "connecting"))
                if listener:
                    self._placeholder("Waiting for the receiver…",
                                      f"Listening on port {self.vars['port'].get()}. The receiver should connect to\n"
                                      f"{receiver_url(self.settings()).split('?')[0]}")
                else:
                    self._placeholder("Connecting…", f"Calling {self.vars['host'].get()}:{self.vars['port'].get()}")
                if not self.retry_count:
                    self._set_msg("Connecting…")
            else:
                self.want_stream = False
        elif self.want_preview:
            if self._spawn("preview"):
                self.pill.set("preview")
                self._placeholder("Starting preview…", "")
            else:
                self.want_preview = False
        else:
            self.pill.set("offline")
            self._placeholder("Preview is off", "Click Preview to check your source before going live.")
        if not self.want_stream:
            for k in ("bitrate", "fps", "drop", "uptime"):
                self.stat_vars[k].set("—")
                self.stat_vars[k + "_sub"].set("")
            self.uptime_hdr.configure(text="")
            self.spark.set([], 1)
        self._update_controls()

    def _spawn(self, kind):
        s = self.settings()
        video, audio = self.selected_sources()
        if not video:
            self._set_msg("Pick a video source first.", "error")
            return False
        preview = self._preview_size()
        try:
            cmd = build_command(s, video, audio, stream=(kind == "stream"), preview=preview)
            shown = build_command(s, video, audio, mask_secrets=True, stream=(kind == "stream"), preview=preview)
        except (ValueError, KeyError) as e:
            self._set_msg(f"Check settings: {e}", "error")
            return False
        self._write_log(("▶ stream" if kind == "stream" else "▶ preview") + ":  " + cmd_to_string(shown), "cmd")
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, creationflags=NO_WINDOW)
        except OSError as e:
            self._set_msg(f"Could not start FFmpeg: {e}. Check its location in Advanced.", "error")
            self._write_log(f"Could not start FFmpeg: {e}", "err")
            self.pill.set("error")
            return False
        self.gen += 1
        self.proc, self.proc_kind = proc, kind
        self.live, self.started_at = False, None
        self.recent_lines.clear()
        self._meter_acc = {}
        self._frame = None
        threading.Thread(target=self._stderr_reader, args=(proc, self.gen), daemon=True).start()
        threading.Thread(target=self._frame_reader, args=(proc, self.gen), daemon=True).start()
        return True

    def _stderr_reader(self, proc, gen):
        buf = b""
        while True:
            chunk = proc.stderr.read1(4096)
            if not chunk:
                break
            buf += chunk
            *lines, buf = re.split(rb"[\r\n]", buf)
            for raw in lines:
                if not raw.strip():
                    continue
                line = raw.decode("utf-8", "replace")
                if "Parsed_astats" in line:
                    continue
                if "Parsed_ametadata" in line:
                    m = METER_RE.search(line)
                    if m and gen == self.gen:
                        ch, val = int(m.group(1)), float(m.group(2))
                        self._meter_acc[ch] = max(self._meter_acc.get(ch, -200.0), val)
                    continue
                self.q.put(("line", (gen, line)))
        if buf.strip():
            self.q.put(("line", (gen, buf.decode("utf-8", "replace"))))
        self.q.put(("exit", (gen, proc.wait())))

    def _frame_reader(self, proc, gen):
        f = proc.stdout
        try:
            while True:
                magic = f.readline()
                if not magic:
                    break
                if magic.strip() != b"P6":
                    continue
                dims, maxval = f.readline(), f.readline()
                w, h = (int(x) for x in dims.split())
                data = f.read(w * h * 3)
                if len(data) < w * h * 3:
                    break
                self._frame = (gen, magic + dims + maxval + data)
        except (ValueError, OSError):
            pass

    def _handle_line(self, gen, line):
        if gen != self.gen:
            return
        m = STATS_RE.search(line)
        if m:
            if self.proc_kind != "stream":
                return
            if not self.live:
                self.live = True
                self.started_at = time.time()
                self.retry_count = 0
                self.pill.set("live")
                self._set_msg(f"You're live → {self.vars['host'].get() or 'receiver'}:{self.vars['port'].get()}", "ok")
                self._write_log("● Stream is live.", "app")
                self._draw_badge()
            fps = m.group(2)
            target = self.vars["fps"].get()
            self.stat_vars["fps"].set(f"{float(fps):.0f}")
            self.stat_vars["fps_sub"].set(f"target {target} · speed {m.group(4)}")
            br = re.match(r"([\d.]+)kbits/s", m.group(3))
            if br:
                kbps = float(br.group(1))
                self.bitrate_hist.append(kbps)
                self.stat_vars["bitrate"].set(f"{kbps / 1000:.2f} Mbps" if kbps >= 1000 else f"{kbps:.0f} kbps")
                target_kbps = float(self.vars["vbitrate"].get() or 0)
                self.stat_vars["bitrate_sub"].set(f"average since start · target {target_kbps / 1000:g} Mbps")
                self.spark.set(self.bitrate_hist, max(max(self.bitrate_hist), target_kbps) * 1.15)
            d = DROP_RE.search(line)
            if d:
                frames = int(m.group(1))
                drops = int(d.group(1))
                self.stat_vars["drop"].set(str(drops))
                pct = 100.0 * drops / max(frames + drops, 1)
                self.stat_vars["drop_sub"].set(f"{pct:.1f}% of {frames + drops} frames")
            return
        self.recent_lines.append(line)
        self._write_log(line, "err" if ERROR_RE.search(line) else None)

    HINTS = (
        ("extends outside window area", "GDI capture can't handle this monitor layout (mixed display "
                                        "scaling). Pick a screen without 'compatibility (GDI)' in Sources."),
        ("ddagrab", "GPU screen capture failed. On laptops with two graphics chips, this screen may be "
                    "driven by the other GPU. Try the compatibility (GDI) option or a different screen."),
        ("Connection setup failure", "No receiver answered. Check the host, port, firewall, and that the "
                                     "receiver is listening (and uses the same passphrase)."),
        ("Address already in use", "That port is already in use on this computer. Pick another port."),
    )

    def _exit_reason(self, rc):
        for needle, hint in self.HINTS:
            for line in self.recent_lines:
                if needle in line and (needle != "ddagrab" or ERROR_RE.search(line.split("]", 1)[-1])):
                    return hint
        for line in reversed(self.recent_lines):
            if ERROR_RE.search(line):
                return re.sub(r"^\[[^\]]+\]\s*", "", line).strip()
        return f"FFmpeg exited with code {rc}."

    def _on_proc_exit(self, gen, rc):
        if gen != self.gen:
            return
        kind = self.proc_kind
        self.proc, self.proc_kind, self.live = None, None, False
        if self.pending:
            cb, self.pending = self.pending, None
            cb()
            return
        if rc > 2 ** 31:
            rc -= 2 ** 32  # Windows reports negative exit codes as unsigned
        reason = self._exit_reason(rc)
        self._write_log(f"FFmpeg stopped (code {rc}).", "err")
        if kind == "stream" and self.want_stream:
            if self.vars["reconnect"].get():
                self.retry_count += 1
                delay = min(2 ** self.retry_count, 30)
                self.pill.set("reconnecting")
                self._placeholder("Connection lost", f"Retrying in {delay} s (attempt {self.retry_count})…")
                self._set_msg(f"{reason}  Retrying in {delay} s…", "warn")
                self.retry_job = self.after(delay * 1000, self._sync)
                self._update_controls()
                return
            self.want_stream = False
            self._set_msg(f"Stream stopped: {reason}", "error")
        elif kind == "preview":
            self.want_preview = False
            self._set_msg(f"Preview stopped: {reason}", "error")
        self._toggle_log(True)
        self._sync()
        if not self.proc:
            self.pill.set("error")

    # ------------------------------------------------------------- main loop
    def _tick(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "line":
                    self._handle_line(*payload)
                elif kind == "exit":
                    self._on_proc_exit(*payload)
                elif kind == "devices":
                    self._apply_devices(*payload)
                elif kind == "ffcheck":
                    self._apply_ffcheck(*payload)
        except queue.Empty:
            pass

        now = time.time()
        dt, self._last_tick = now - self._last_tick, now

        fr = self._frame
        if fr is not None and fr is not self._shown_frame and self.proc and fr[0] == self.gen:
            self._shown_frame = fr
            try:
                self._photo = tk.PhotoImage(data=fr[1], format="PPM")
                self.canvas.itemconfigure(self.img_item, image=self._photo, state="normal")
                self.canvas.itemconfigure(self.ph_title, state="hidden")
                self.canvas.itemconfigure(self.ph_sub, state="hidden")
                self._draw_badge()
            except tk.TclError:
                pass

        acc, self._meter_acc = self._meter_acc, {}
        if acc and self.proc:
            left = acc.get(1, -90.0)
            peaks = (left, acc.get(2, left))
        else:
            peaks = (-90.0, -90.0)
        if acc or max(self.meter.level) > -60 or max(self.meter.hold) > -59:
            self.meter.update_levels(peaks, dt)

        if self.live and self.started_at:
            secs = int(now - self.started_at)
            up = f"{secs // 3600:d}:{secs // 60 % 60:02d}:{secs % 60:02d}"
            self.stat_vars["uptime"].set(up)
            self.stat_vars["uptime_sub"].set(f"since {time.strftime('%H:%M', time.localtime(self.started_at))}")
            self.uptime_hdr.configure(text=up)
        if now - self._last_blink > 0.6:
            self._last_blink = now
            self.pill.toggle_blink()
        self.after(40, self._tick)

    def _on_close(self):
        if self.want_stream and self.live and not messagebox.askyesno(APP_NAME, "You're live. End the stream and quit?"):
            return
        save_config(self.settings())
        self.want_stream = self.want_preview = False
        self._cancel_retry()
        if self.proc:
            graceful_stop(self.proc, timeout=3)
        self.destroy()


def main():
    if "--self-test" in sys.argv:
        import selftest
        return selftest.main(sys.argv[sys.argv.index("--self-test") + 1:])
    App().mainloop()


if __name__ == "__main__":
    sys.exit(main())
