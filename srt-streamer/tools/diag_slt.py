"""Diagnostic: FFmpeg -> srt-live-transmit -> receiver, in several variants, reporting how many stats
reports srt-live-transmit wrote and what the receiver got. All process output goes to files (no pipes),
so nothing can block on a full pipe.

    python tools/diag_slt.py <ffmpeg> <srt-live-transmit> <out-dir>
"""
import json
import os
import socket
import subprocess
import sys
import time

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def free_port(kind):
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def spawn(cmd, out, name):
    return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=open(os.path.join(out, name + ".out"), "wb"),
                            stderr=open(os.path.join(out, name + ".err"), "wb"), creationflags=NO_WINDOW)


def read(out, name, n=600):
    try:
        with open(os.path.join(out, name), "rb") as f:
            data = f.read()
        return len(data), data.count(b"{"), data[-n:].decode("utf-8", "replace")
    except OSError:
        return 0, 0, ""


def stop(p):
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()


def variant(ff, slt, out, name, stats=True, receiver="ffmpeg", sender="slt"):
    d = os.path.join(out, name)
    os.makedirs(d, exist_ok=True)
    srt_port, udp_port = free_port(socket.SOCK_DGRAM), free_port(socket.SOCK_DGRAM)
    rx_ts = os.path.join(d, "rx.ts")
    if receiver == "ffmpeg":
        rx = spawn([ff, "-hide_banner", "-loglevel", "warning", "-y", "-i",
                    f"srt://127.0.0.1:{srt_port}?mode=listener", "-c", "copy", rx_ts], d, "rx")
    else:
        rx_ts = os.path.join(d, "rx.out")  # srt-live-transmit can only write to stdout (file://con)
        rx = spawn([slt, f"srt://:{srt_port}?mode=listener", "file://con"], d, "rx")
    time.sleep(1.5)
    gen = [ff, "-hide_banner", "-loglevel", "warning", "-re", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
           "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "5",
           "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "1M", "-c:a", "aac", "-b:a", "128k", "-f", "mpegts"]
    sl = None
    if sender == "slt":
        flags = ["-s:20", "-pf:json", "-f"] if stats else []
        sl = spawn([slt] + flags + ["-a:no", f"udp://127.0.0.1:{udp_port}",
                                    f"srt://127.0.0.1:{srt_port}?mode=caller&latency=200"], d, "slt")
        time.sleep(1.0)
        tx = subprocess.run(gen + [f"udp://127.0.0.1:{udp_port}?pkt_size=1316"], stdin=subprocess.DEVNULL,
                            capture_output=True, creationflags=NO_WINDOW, timeout=60)
    else:
        tx = subprocess.run(gen + [f"srt://127.0.0.1:{srt_port}?mode=caller&pkt_size=1316"],
                            stdin=subprocess.DEVNULL, capture_output=True, creationflags=NO_WINDOW, timeout=60)
    time.sleep(2.0)
    stop(sl)
    time.sleep(1.0)
    stop(rx)
    probe = subprocess.run([ff, "-hide_banner", "-i", rx_ts], capture_output=True, text=True, errors="replace",
                           stdin=subprocess.DEVNULL, creationflags=NO_WINDOW).stderr
    rx_size = os.path.getsize(rx_ts) if os.path.exists(rx_ts) else 0
    so_len, reports, _ = read(d, "slt.out")
    _, _, se = read(d, "slt.err")
    _, _, rxe = read(d, "rx.err", 400)
    return {"variant": name, "stats_reports": reports, "slt_stdout_bytes": so_len,
            "received_bytes": rx_size, "expected_bytes": "~700000",
            "received_streams": [ln.strip()[:70] for ln in probe.splitlines() if "Stream #" in ln],
            "slt_stderr": se.strip().replace("\n", " / ")[-400:], "rx_stderr": rxe.strip().replace("\n", " / ")[-300:],
            "sender_stderr": tx.stderr[-300:].decode("utf-8", "replace").strip()}


def main():
    ff, slt, out = sys.argv[1], sys.argv[2], sys.argv[3]
    os.makedirs(out, exist_ok=True)
    report = []
    for kw in ({"name": "A-udp-stats"}, {"name": "B-udp-nostats", "stats": False},
               {"name": "C-slt-receiver", "receiver": "slt"}, {"name": "D-ffmpeg-direct", "sender": "ffmpeg"}):
        try:
            report.append(variant(ff, slt, out, **kw))
        except Exception as e:
            report.append({"variant": kw["name"], "error": repr(e)})
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
