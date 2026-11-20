"""Diagnostic: run FFmpeg -> srt-live-transmit -> FFmpeg receiver for a few seconds and report what
srt-live-transmit printed (stdout/stderr, and a -statsout file) and what the receiver got.

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


def run(ff, slt, out, use_file=False, source="udp"):
    srt_port, udp_port = free_port(socket.SOCK_DGRAM), free_port(socket.SOCK_DGRAM)
    tag = ("file" if use_file else "stdout") + "-" + source
    rx_path = os.path.join(out, f"diag-rx-{tag}.ts")
    stats_file = os.path.join(out, f"diag-stats-{tag}.txt")
    rx = subprocess.Popen([ff, "-hide_banner", "-loglevel", "warning", "-y", "-i",
                           f"srt://127.0.0.1:{srt_port}?mode=listener", "-c", "copy", rx_path],
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                          creationflags=NO_WINDOW)
    time.sleep(1.5)
    src = {"udp": f"udp://127.0.0.1:{udp_port}?rcvbuf=8388608", "udp-plain": f"udp://127.0.0.1:{udp_port}",
           "stdin": "file://con"}[source]
    cmd = [slt, "-s:20", "-pf:json", "-f", "-a:no"] + ([f"-statsout:{stats_file}"] if use_file else []) + \
          [src, f"srt://127.0.0.1:{srt_port}?mode=caller&latency=200"]
    gen = [ff, "-hide_banner", "-loglevel", "warning", "-re",
           "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
           "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "5",
           "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "1M", "-c:a", "aac", "-b:a", "128k", "-f", "mpegts"]
    if source == "stdin":
        txp = subprocess.Popen(gen + ["pipe:1"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, creationflags=NO_WINDOW)
        sl = subprocess.Popen(cmd, stdin=txp.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              creationflags=NO_WINDOW)
        txp.stdout.close()
        tx_err = txp.communicate(timeout=60)[1]
    else:
        sl = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              creationflags=NO_WINDOW)
        time.sleep(1.0)
        tx_err = subprocess.run(gen + [f"udp://127.0.0.1:{udp_port}?pkt_size=1316&buffer_size=8388608"],
                                stdin=subprocess.DEVNULL, capture_output=True, creationflags=NO_WINDOW,
                                timeout=60).stderr
    time.sleep(1.5)
    sl.terminate()
    try:
        so, se = sl.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        sl.kill()
        so, se = sl.communicate()
    time.sleep(1.0)
    rx.terminate()
    try:
        rx_err = rx.communicate(timeout=10)[1]
    except subprocess.TimeoutExpired:
        rx.kill()
        rx_err = rx.communicate()[1]
    probe = subprocess.run([ff, "-hide_banner", "-i", rx_path], capture_output=True, text=True, errors="replace",
                           stdin=subprocess.DEVNULL, creationflags=NO_WINDOW).stderr
    streams = [ln.strip() for ln in probe.splitlines() if "Stream #" in ln]
    stats_text = open(stats_file, errors="replace").read() if os.path.exists(stats_file) else None
    return {"mode": tag, "cmd": cmd, "slt_stdout_bytes": len(so), "slt_stdout_head": so[:700].decode("utf-8", "replace"),
            "slt_stderr": se[-1500:].decode("utf-8", "replace"),
            "stats_file_bytes": None if stats_text is None else len(stats_text),
            "stats_file_head": (stats_text or "")[:700], "sender_stderr": tx_err[-800:].decode("utf-8", "replace"),
            "reports": so.count(b"{") + (stats_text or "").count("{"),
            "receiver_stderr": rx_err[-800:].decode("utf-8", "replace"), "received_streams": streams}


def main():
    ff, slt, out = sys.argv[1], sys.argv[2], sys.argv[3]
    os.makedirs(out, exist_ok=True)
    report = [run(ff, slt, out, source="udp"), run(ff, slt, out, source="udp-plain"),
              run(ff, slt, out, source="stdin"), run(ff, slt, out, use_file=True, source="stdin")]
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
