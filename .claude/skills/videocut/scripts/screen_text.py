# /// script
# requires-python = ">=3.10,<3.13"
# dependencies = ["rapidocr_onnxruntime>=1.3,<2", "numpy<2"]
# ///
"""识别录屏画面上的文字（本地 OCR，不上传），给专有名词校对当证据。

识别结果只能告诉你"听起来像什么"，画面上写着的才是正确拼写：讲解时屏幕上通常就开着文章、
文档、代码或幻灯片（例如画面写 Router，识别却时而 root 时而 router）。

用法：
  uv run screen_text.py --project "<xxx.focusee>"            # 每 4 秒抽一帧做 OCR，画面没变的帧跳过（有缓存）
  uv run screen_text.py --project "<xxx.focusee>" --at 680   # 打印某个时间点前后画面上的文字
  uv run screen_text.py --project "<xxx.focusee>" --find router,root   # 查这些词在画面上出现过没有、什么写法

输出：工作目录里的 screen_text.json（每帧文字）和 screen_terms.txt（画面上出现过的英文/数字术语及次数）。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fsproj  # noqa: E402

WIDTH = 1600
TERM_RE = re.compile(r"[A-Za-z][A-Za-z0-9.+#_'-]*(?:[ -][A-Z0-9][A-Za-z0-9.+#_'-]*)*")


def frames(video: Path, step: float, duration: float):
    """用 ffmpeg 按固定间隔抽灰度小图判断变化、彩色大图做 OCR。"""
    h = None
    probe = subprocess.run([fsproj.ffbin("ffprobe"), "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "csv=p=0", str(video)], capture_output=True, text=True)
    w0, h0 = [int(x) for x in probe.stdout.strip().split(",")[:2]]
    h = int(round(h0 * WIDTH / w0 / 2) * 2)
    cmd = [fsproj.ffbin("ffmpeg"), "-v", "error", "-i", str(video), "-vf", f"fps=1/{step},scale={WIDTH}:{h}",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    size = WIDTH * h * 3
    i = 0
    while True:
        buf = p.stdout.read(size)
        if len(buf) < size:
            break
        yield i * step, np.frombuffer(buf, np.uint8).reshape(h, WIDTH, 3)
        i += 1
    p.wait()


def run_ocr(pr, wd: Path, step: float) -> list[dict]:
    from rapidocr_onnxruntime import RapidOCR
    ocr = RapidOCR()
    video = fsproj.display_video(pr)
    out, prev = [], None
    for t, img in frames(video, step, pr.duration):
        small = img[::16, ::16].mean(axis=2)
        if prev is not None and np.abs(small - prev).mean() < 1.5:  # 画面基本没变
            out[-1]["until"] = round(t, 1)
            continue
        prev = small
        res, _ = ocr(img)
        lines = [r[1].strip() for r in (res or []) if r[2] > 0.6 and r[1].strip()]
        out.append({"t": round(t, 1), "until": round(t, 1), "lines": lines})
        print(f"{t:7.1f}s {len(lines):3d} 行", file=sys.stderr, flush=True)
    return out


def terms_of(data: list[dict]) -> Counter:
    c = Counter()
    for f in data:
        seen = set()
        for ln in f["lines"]:
            for m in TERM_RE.findall(ln):
                m = m.strip(" .-'")
                if len(m) >= 2 and m not in seen:
                    seen.add(m)
                    c[m] += 1
    return c


def main():
    fsproj.utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project")
    ap.add_argument("--step", type=float, default=4.0, help="抽帧间隔（秒）")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--at", type=float, help="打印这个时间点（源时间秒）前后画面上的文字")
    ap.add_argument("--find", help="逗号分隔的词，查它们在画面上的写法（不区分大小写）")
    args = ap.parse_args()
    pr = fsproj.load(args.project)
    wd = fsproj.work_dir(pr)
    cache = wd / "screen_text.json"
    if cache.exists() and not args.force:
        data = json.loads(cache.read_text("utf-8"))["frames"]
    else:
        data = run_ocr(pr, wd, args.step)
        cache.write_text(json.dumps({"step": args.step, "frames": data}, ensure_ascii=False, indent=1), "utf-8")
        terms = terms_of(data)
        (wd / "screen_terms.txt").write_text(
            "# 录屏画面上出现过的英文/数字术语（帧数），校对专有名词时以这里的写法为准\n"
            + "\n".join(f"{k}\t{v}" for k, v in terms.most_common()), "utf-8")

    if args.at is not None:
        near = [f for f in data if f["t"] - 6 <= args.at <= f["until"] + 6]
        for f in near:
            print(f"--- {f['t']}s ~ {f['until']}s")
            print("\n".join(f["lines"]))
        return
    if args.find:
        for w in [x.strip() for x in args.find.split(",") if x.strip()]:
            pat = re.compile(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", re.I)
            hits = Counter()
            times = []
            for f in data:
                for ln in f["lines"]:
                    for m in pat.findall(ln):
                        hits[m] += 1
                        times.append(f["t"])
            print(f"{w}: " + (", ".join(f"{k}×{v}" for k, v in hits.most_common()) or "画面上没出现")
                  + (f"（首次 {min(times):.0f}s）" if times else ""))
        return
    terms = terms_of(data)
    print(json.dumps({"screen_text": str(cache), "screen_terms": str(wd / "screen_terms.txt"), "frames": len(data),
                      "top_terms": [k for k, _ in terms.most_common(60)]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
