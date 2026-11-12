# /// script
# requires-python = ">=3.10"
# ///
"""生成审阅页用的低清预览视频 proxy.mp4（1280 宽，带识别所用的音轨，每秒一个关键帧方便拖动）。

用法：uv run make_proxy.py --project "<xxx.focusee>" [--width 1280] [--force]
优先用 NVIDIA 硬件编码 h264_nvenc，失败时回退 libx264。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fsproj  # noqa: E402


def run(cmd) -> tuple[bool, str]:
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode == 0, r.stderr[-1500:]


def main():
    fsproj.utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--project")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    pr = fsproj.load(args.project)
    wd = fsproj.work_dir(pr)
    out = wd / "proxy.mp4"
    if out.exists() and not args.force:
        print(json.dumps({"proxy": str(out), "cached": True}, ensure_ascii=False))
        return
    video = fsproj.display_video(pr)
    audio = fsproj.pick_audio(pr)
    tmp = wd / "proxy.tmp.mp4"
    ff = fsproj.ffbin("ffmpeg")

    inputs = ["-i", str(video)]
    if audio == video:
        maps = ["-map", "0:v:0", "-map", "0:a:0"]
    else:
        inputs += ["-i", str(audio)]
        maps = ["-map", "0:v:0", "-map", "1:a:0"]
    common_tail = ["-vf", f"scale={args.width}:-2", "-g", "30", "-keyint_min", "30",
                   "-c:a", "aac", "-b:a", "96k", "-ac", "1", "-movflags", "+faststart", str(tmp)]

    t0 = time.time()
    attempts = [
        ("h264_nvenc", ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "32", "-b:v", "0"]),
        ("libx264", ["-c:v", "libx264", "-preset", "veryfast", "-crf", "30"]),
    ]
    for k, (name, venc) in enumerate(attempts):
        cmd = [ff, "-y", "-v", "error", *inputs, *maps, *venc, *common_tail]
        print(f"编码预览视频（{name}）…", file=sys.stderr)
        ok, err = run(cmd)
        if not ok:
            last_line = err.strip().splitlines()[-1] if err.strip() else ""
            print(f"  {name} 失败{'，改用下一个编码器' if k + 1 < len(attempts) else ''}：{last_line}", file=sys.stderr)
            if k + 1 == len(attempts):
                print(err, file=sys.stderr)
        if ok:
            tmp.replace(out)
            print(json.dumps({"proxy": str(out), "encoder": name, "seconds": round(time.time() - t0, 1),
                              "size_mb": round(out.stat().st_size / 1e6, 1)}, ensure_ascii=False))
            return
    sys.exit("预览视频生成失败")


if __name__ == "__main__":
    main()
