"""
End-to-end self-test for SRT Streamer.

Drives the real UI: device discovery -> preview (frames + audio meters) -> go live with
encryption to a local SRT receiver -> stream health -> end stream. The receiver is a
separate FFmpeg process; its recording is checked for H.264 video and AAC audio.

Usage (works with the source or the built exe):
    python srt_streamer.py --self-test report.json [--capture ddagrab|gdigrab|x11grab|auto]
    "SRT Streamer.exe"   --self-test report.json --capture gdigrab

Exit code 0 = all checks passed. A JSON report (and a screenshot, when possible) is written
next to the report path.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback

import srt_streamer as m

PORT = 9177
PASSPHRASE = "selftest-passphrase"
LIVE_SECONDS = 30  # cap; the receiver also stops when the sender disconnects
HARD_TIMEOUT = 120


def _test_tone_patch():
    """Add a synthetic stereo tone 'device' that goes through the same PcmPump/TCP path as
    Windows computer sound, so that path and the audio meters are exercised on every platform."""
    orig_enum = m.enumerate_sources

    def enum(ff):
        video, audio, notes = orig_enum(ff)
        return video, audio + [{"label": "Self-test tone", "type": "pcm_pump", "fmt": "f32le",
                                "rate": 48000, "channels": 2, "make_source": m.ToneSource}], notes

    m.enumerate_sources = enum


def _probe(ff, path):
    out = subprocess.run([ff, "-hide_banner", "-i", path], capture_output=True, text=True,
                         errors="replace", stdin=subprocess.DEVNULL,
                         creationflags=m.NO_WINDOW).stderr
    video = re.search(r"Video: (\w+).*?(\d{2,5})x(\d{2,5})", out)
    audio = re.search(r"Audio: (\w+)", out)
    return {"video": video.group(1) if video else None,
            "size": f"{video.group(2)}x{video.group(3)}" if video else None,
            "audio": audio.group(1) if audio else None}


def main(argv):
    report_path = os.path.abspath(argv[0] if argv and not argv[0].startswith("--") else "selftest-report.json")
    capture = argv[argv.index("--capture") + 1] if "--capture" in argv else "auto"
    out_dir = os.path.dirname(report_path)
    os.makedirs(out_dir, exist_ok=True)
    m.CONFIG_PATH = os.path.join(tempfile.gettempdir(), "srt_streamer_selftest.json")
    if os.path.exists(m.CONFIG_PATH):  # always start from defaults, never a previous run's settings
        os.remove(m.CONFIG_PATH)
    m.DEFAULTS["ffmpeg"] = m.find_ffmpeg()
    m.DEFAULTS["srt_lib"] = m.find_libsrt()
    m.messagebox.askyesno = lambda *a, **k: True
    _test_tone_patch()

    report = {"version": m.APP_VERSION, "platform": m.platform.platform(), "capture": capture,
              "checks": {}, "info": {}, "ok": False}
    checks, info = report["checks"], report["info"]
    received = os.path.join(out_dir, f"received-{capture}.ts")
    if os.path.exists(received):
        os.remove(received)
    state = {"receiver": None, "t0": time.time()}
    app = m.App()

    def on_callback_error(exc, val, tb):  # any exception inside a Tk callback fails the test
        errors = info.setdefault("callback_errors", [])
        errors.append("".join(traceback.format_exception(exc, val, tb))[-1500:])
        check("no_ui_errors", False, f"{len(errors)} error(s); first: {errors[0][-300:]}")
    app.report_callback_exception = on_callback_error

    def check(name, ok, detail=""):
        checks[name] = {"ok": bool(ok), "detail": str(detail)}

    def finish():
        try:
            rx = state["receiver"]
            if rx:
                try:
                    rx.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    rx.kill()
            if os.path.exists(received):
                probe = _probe(app.vars["ffmpeg"].get(), received)
                info["received"] = probe
                check("receiver_got_h264", probe["video"] == "h264", probe)
                check("receiver_got_aac", probe["audio"] == "aac", probe)
            else:
                check("receiver_got_h264", False, "receiver wrote no file")
        finally:
            checks.setdefault("no_ui_errors", {"ok": True, "detail": ""})
            report["ok"] = bool(checks) and all(c["ok"] for c in checks.values())
            report["seconds"] = round(time.time() - state["t0"], 1)
            with open(report_path, "w") as f:
                json.dump(report, f, indent=2)
            app.after(10, app.destroy)

    def fail(msg):
        info["error"] = msg
        info["log_tail"] = list(app.recent_lines)[-15:]
        app.want_stream = app.want_preview = False
        app._stop_proc(then=finish)

    def screenshot(name):
        try:
            app.update()
            x, y, w, h = app.winfo_rootx(), app.winfo_rooty(), app.winfo_width(), app.winfo_height()
            path = os.path.join(out_dir, f"{name}-{capture}.png")
            ff = app.vars["ffmpeg"].get()
            if m.SYSTEM == "Windows":
                grab = ["-f", "gdigrab", "-offset_x", str(x), "-offset_y", str(y),
                        "-video_size", f"{w - w % 2}x{h - h % 2}", "-i", "desktop"]
            else:
                grab = ["-f", "x11grab", "-video_size", f"{w - w % 2}x{h - h % 2}",
                        "-i", f"{os.environ.get('DISPLAY', ':0')}+{x},{y}"]
            subprocess.run([ff, "-loglevel", "error", "-y"] + grab + ["-frames:v", "1", path],
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=20,
                           creationflags=m.NO_WINDOW)
        except Exception as e:  # screenshots are best-effort
            info[f"screenshot_{name}"] = f"failed: {e}"

    def wait_for(cond, timeout, then, on_timeout):
        deadline = time.time() + timeout

        def poll():
            try:
                if cond():
                    then()
                elif time.time() > deadline:
                    on_timeout()
                else:
                    app.after(200, poll)
            except Exception:
                info["exception"] = traceback.format_exc()
                fail("exception during self-test")
        poll()

    # ---- steps ---------------------------------------------------------------
    def step_devices():
        ok_ff = app.ff_status.get().startswith("✓")
        check("ffmpeg_with_srt", ok_ff, app.ff_status.get())
        info["video_sources"] = list(app.video_map)
        info["ffmpeg"] = app.vars["ffmpeg"].get()
        info["srt_lib"] = app.vars["srt_lib"].get()
        if m.SYSTEM == "Windows":
            try:
                res = {}

                def probe():  # COM apartment setup belongs off the Tk thread, as in the app
                    try:
                        res["outs"] = m.wasapi_output_devices()
                    except Exception as e:
                        res["err"] = e
                t = threading.Thread(target=probe, daemon=True)
                t.start()
                t.join(15)
                if "err" in res or "outs" not in res:
                    raise res.get("err") or TimeoutError("listing outputs timed out")
                outs = res["outs"]
                info["wasapi_outputs"] = [(d["name"], d["fmt"], d["rate"], d["channels"]) for d in outs]
                check("wasapi_enumeration", True, info["wasapi_outputs"])
            except Exception as e:
                check("wasapi_enumeration", False, repr(e))
            try:
                info["dxgi_outputs"] = [(n, list(r)) for n, r, _h in m.dxgi_outputs()]
                check("dxgi_enumeration", True, info["dxgi_outputs"])
            except Exception as e:
                check("dxgi_enumeration", False, repr(e))
        wanted = [lbl for lbl, src in app.video_map.items() if capture in ("auto", src["type"])]
        check("video_source_available", wanted, wanted[:1] or f"no '{capture}' source in {list(app.video_map)}")
        if not wanted or not ok_ff:
            return fail("prerequisites missing")
        app.vars["video"].set(wanted[0])
        app.vars["audio"].set("Self-test tone")
        app.vars["host"].set("127.0.0.1")
        app.vars["port"].set(str(PORT))
        app.vars["passphrase"].set(PASSPHRASE)
        app.vars["mode"].set("caller")
        app.toggle_preview()
        wait_for(lambda: app._photo is not None and max(app.meter.level) > -40, 20, step_preview_ok,
                 lambda: (check("preview_frames_and_meters", False,
                                f"photo={app._photo is not None} meter={app.meter.level}"),
                          fail("preview did not start")))

    def step_preview_ok():
        check("preview_frames_and_meters", True, f"meter={[round(v, 1) for v in app.meter.level]}")
        screenshot("preview")
        ff = app.vars["ffmpeg"].get()
        state["receiver"] = subprocess.Popen(
            [ff, "-hide_banner", "-y", "-i",
             f"srt://127.0.0.1:{PORT}?mode=listener&passphrase={PASSPHRASE}&pbkeylen=16",
             "-t", str(LIVE_SECONDS), "-c", "copy", received],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=m.NO_WINDOW)
        app.after(1500, step_go_live)

    def step_go_live():
        app.toggle_live()
        def streaming():
            try:
                return app.live and float(app.stat_vars["fps"].get()) > 0
            except ValueError:
                return False
        wait_for(streaming, 25, step_live_ok,
                 lambda: (check("went_live", False, f"pill={app.pill.state} msg={app.msg.get()}"),
                          fail("did not go live")))

    def step_live_ok():
        check("went_live", True, f"fps={app.stat_vars['fps'].get()}")
        info["srt_stats_mode"] = app.srt_mode
        if app.srt_mode:  # detailed SRT statistics from libsrt
            wait_for(lambda: app.stat_vars["q"].get() in ("Good", "Fair", "Poor")
                     and app.stat_vars["rtt"].get() != "—" and app.stat_vars["bitrate"].get() != "—",
                     12, lambda: step_stats(True), lambda: step_stats(False))
        else:  # encoder bandwidth only
            wait_for(lambda: app.stat_vars["bitrate"].get() != "—", 12,
                     lambda: step_stats(True), lambda: step_stats(False))

    def step_stats(ok):
        sv = {k: v.get() for k, v in app.stat_vars.items()}
        detail = {k: sv[k] for k in ("bitrate", "q", "q_sub", "rtt", "loss", "retx", "pdrop", "buf")}
        check("srt_link_stats" if app.srt_mode else "bandwidth_stats", ok, detail)
        app.after(2500, lambda: (screenshot("live"), app.after(500, step_end)))

    def step_end():
        app.toggle_live()
        wait_for(lambda: app.proc_kind == "preview" and not app.want_stream, 15, step_back_to_preview,
                 lambda: (check("returned_to_preview", False, f"proc={app.proc_kind}"), fail("end failed")))

    def step_back_to_preview():
        check("returned_to_preview", True)
        app.toggle_preview()
        wait_for(lambda: app.proc is None, 15,
                 lambda: (check("stopped_cleanly", True), step_link_test()),
                 lambda: (check("stopped_cleanly", False, f"proc={app.proc_kind}"), fail("stop failed")))

    def step_link_test():
        """Bandwidth analyzer: short SRT link test to a local receiver, then apply the recommendation."""
        if not m.check_srt_lib(app.vars["srt_lib"].get()):
            return finish()
        rx = state["receiver"]
        if rx and rx.poll() is None:
            rx.wait(timeout=10)
        m.LINK_TEST_STEPS[:] = [1, 2, 4]
        ff = app.vars["ffmpeg"].get()
        state["receiver2"] = subprocess.Popen(
            [ff, "-hide_banner", "-loglevel", "quiet", "-i",
             f"srt://127.0.0.1:{PORT}?mode=listener&passphrase={PASSPHRASE}&pbkeylen=16", "-f", "null", "-"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=m.NO_WINDOW)
        app.tabbar.select("bandwidth")
        app.vars["content"].set("film")
        app.after(1500, app.toggle_link_test)
        wait_for(lambda: app.bw_test is None and app.bw_measured is not None, 45, step_link_done,
                 lambda: (check("bandwidth_link_test", False, app.bw_progress.get()), fail("link test failed")))

    def step_link_done():
        meas = app.bw_measured
        check("bandwidth_link_test", meas["capacity_kbps"] >= 1500,
              f"{app.bw_progress.get()} | budget={app.vars['budget'].get()}")
        screenshot("bandwidth")
        rec = app.bw_rec
        before = app.vars["vbitrate"].get()
        app.apply_recommendation()
        ok = bool(rec) and app.vars["vbitrate"].get() == str(rec[1]) and app.vars["fps"].get() == str(rec[0]["fps"])
        check("bandwidth_apply", ok, f"{app.bw_summary.get()} | vbitrate {before} -> {app.vars['vbitrate'].get()}, "
                                     f"scale={app.vars['scale'].get()} fps={app.vars['fps'].get()} "
                                     f"latency={app.vars['latency'].get()}")
        rx2 = state.get("receiver2")
        if rx2 and rx2.poll() is None:
            rx2.kill()
        finish()

    def hard_timeout():
        if not os.path.exists(report_path) or os.path.getmtime(report_path) < state["t0"]:
            check("finished_in_time", False, f"exceeded {HARD_TIMEOUT}s")
            fail("hard timeout")

    wait_for(lambda: app.video_map and not app.ff_status.get().startswith("Checking"), 30, step_devices,
             lambda: (check("device_discovery", False, "timed out"), fail("device discovery timed out")))
    app.after(HARD_TIMEOUT * 1000, hard_timeout)
    app.mainloop()
    if state["receiver"] and state["receiver"].poll() is None:
        state["receiver"].kill()
    if not os.path.exists(report_path):
        return 2
    with open(report_path) as f:
        return 0 if json.load(f)["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
