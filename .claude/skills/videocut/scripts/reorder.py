# /// script
# requires-python = ">=3.10"
# ///
"""调换顺序：把素材按新顺序重新拼一份，另存成一个新工程（原工程不动）。

为什么要重拼：FocuSee 打开/保存工程时会把 playTracks 按源时间重新排好（已实测），
只改剪辑轨没法让后面说的话先播。所以这里直接改「源」：
  - recording 里的录屏、摄像头、麦克风、系统声音按新顺序切开再拼起来（重新编码，NVIDIA 硬件编码优先）；
  - 鼠标移动/点击/按键记录的时间跟着挪（FocuSee 的光标和自动缩放靠它）；
  - 剪辑轨、字幕、缩放/标注轨换算到新时间；被剪掉的部分也跟着一起挪，所以在 FocuSee 里照样能还原；
  - 新工程用新的 uniqueID，工作目录（逐字稿、剪辑方案、预览视频）也换算好一份，审阅页可以直接接着用。

调换顺序的记录在原工程工作目录的 edit.json → moves（审阅页逐字稿里拖句首的 ⠿，或 analyze.py move）。

用法：uv run reorder.py --project "<xxx.focusee>" [--name 新工程名] [--dry-run] [--edit edit.json]
输出最后一行：DONE {"project": 新工程路径, ...}
"""
from __future__ import annotations

import argparse
import bisect
import copy
import json
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze  # noqa: E402
import fsproj  # noqa: E402

Range = tuple[float, float]
GRID = 1 / 30          # 切点对齐到 30fps 的帧边界，音画各自切出来的长度才一致
MEDIA_EXT = {".mp4", ".m4a", ".mov", ".wav", ".mp3", ".aac"}
SKIP_SUFFIX = (".original.mp4", ".beauty-tmp.mp4")


# ---------------------------------------------------------------- 时间换算

def snap(t: float | None, total: float) -> float | None:
    if t is None:
        return None
    if t <= 0 or t >= total:
        return t
    return round(round(t / GRID) * GRID, 6)


def plan_blocks(moves: list[dict], total: float) -> list[Range]:
    """整条源时间轴按调换顺序切成块，返回新顺序下的块列表（首尾相接覆盖 [0, total]）。"""
    ms = []
    for m in moves:
        if m.get("enabled") is False:
            continue
        mm = dict(m)
        mm["start"], mm["end"] = snap(float(m["start"]), total), snap(float(m["end"]), total)
        mm["before"] = snap(None if m.get("before") is None else float(m["before"]), total)
        ms.append(mm)
    return fsproj.order_keeps([(0.0, total)], ms, total)


class Mapper:
    """源时间 → 新工程时间（块内平移）。"""

    def __init__(self, blocks: list[Range], total: float):
        self.blocks = blocks
        self.total = total
        self.new_start = []
        acc = 0.0
        for a, b in blocks:
            self.new_start.append(acc)
            acc += b - a
        self.order = sorted(range(len(blocks)), key=lambda i: blocks[i][0])
        self.starts = [blocks[i][0] for i in self.order]

    def block_of(self, t: float) -> int:
        j = bisect.bisect_right(self.starts, t) - 1
        return self.order[max(0, j)]

    def __call__(self, t: float) -> float:
        if t <= 0 or t >= self.total:
            return t
        i = self.block_of(t)
        return self.new_start[i] + t - self.blocks[i][0]

    def range(self, a: float, b: float) -> list[Range]:
        """源区间 → 新时间里的若干段（跨块时会拆开），按新时间排序。"""
        out = []
        for i, (x, y) in enumerate(self.blocks):
            lo, hi = max(a, x), min(b, y)
            if hi > lo:
                out.append((self.new_start[i] + lo - x, self.new_start[i] + hi - x))
        if not out and b <= a:  # 零长度（比如某个点）
            t = self(a)
            out.append((t, t))
        return sorted(out)


# ---------------------------------------------------------------- 媒体

_BAD_ENCODERS: set[str] = set()  # 本次运行里已经试过不能用的编码器

def probe_streams(path: Path) -> dict:
    r = subprocess.run([fsproj.ffbin("ffprobe"), "-v", "error", "-show_entries",
                        "stream=index,codec_type,r_frame_rate,sample_rate,channels", "-of", "json", str(path)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    info = {"video": None, "audio": None}
    for s in json.loads(r.stdout or "{}").get("streams", []):
        if s.get("codec_type") in info and info[s["codec_type"]] is None:
            info[s["codec_type"]] = s
    return info


def fps_of(s: dict) -> float:
    try:
        n, d = s.get("r_frame_rate", "30/1").split("/")
        return float(n) / float(d) or 30.0
    except Exception:
        return 30.0


def media_duration(path: Path) -> float:
    r = subprocess.run([fsproj.ffbin("ffprobe"), "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def reorder_media(src: Path, dst: Path, blocks: list[Range], total: float, label: str = "",
                  quality: str = "high") -> str:
    """按块顺序把 src 切开重拼到 dst。每块单独作为一路输入（输入端精确 seek），再用 concat 滤镜接起来。

    源时间最后那一块：如果在新顺序里也是最后，就一直读到文件结尾（各轨原本的尾巴长度不同，保持原样）；
    否则只取到 total，保证后面的块音画对齐。返回实际用的视频编码器。
    """
    st = probe_streams(src)
    has_v, has_a = st["video"] is not None, st["audio"] is not None
    if not (has_v or has_a):
        shutil.copy2(src, dst)
        return "copy"
    cmd = [fsproj.ffbin("ffmpeg"), "-y", "-v", "error", "-nostats", "-progress", "pipe:1"]
    last = len(blocks) - 1
    for i, (a, b) in enumerate(blocks):
        if a > 0:
            cmd += ["-ss", f"{a:.6f}"]
        if not (b >= total and i == last):
            cmd += ["-t", f"{b - a:.6f}"]
        cmd += ["-i", str(src)]
    parts, chains = [], []
    for i in range(len(blocks)):
        if has_v:
            chains.append(f"[{i}:v:0]setpts=PTS-STARTPTS[v{i}]")
            parts.append(f"[v{i}]")
        if has_a:
            chains.append(f"[{i}:a:0]asetpts=PTS-STARTPTS[a{i}]")
            parts.append(f"[a{i}]")
    outs = ("[v]" if has_v else "") + ("[a]" if has_a else "")
    fc = ";".join(chains) + ";" + "".join(parts) + f"concat=n={len(blocks)}:v={int(has_v)}:a={int(has_a)}{outs}"
    cmd += ["-filter_complex", fc]
    if has_v:
        cmd += ["-map", "[v]"]
    if has_a:
        cmd += ["-map", "[a]"]
    acodec = []
    if has_a:
        a = st["audio"]
        acodec = ["-c:a", "aac", "-b:a", "192k", "-ar", str(a.get("sample_rate") or 44100),
                  "-ac", str(a.get("channels") or 2)]
    tail = acodec + ["-movflags", "+faststart", str(dst)]

    def run(vcodec):
        t0, last_print = time.time(), 0.0
        full = cmd + vcodec + tail
        with subprocess.Popen(full, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              encoding="utf-8", errors="replace") as p:
            err_lines = []
            reader = threading.Thread(target=lambda: err_lines.extend(p.stderr), daemon=True)
            reader.start()
            for line in p.stdout:
                if line.startswith("out_time_us=") and time.time() - last_print > 15:
                    try:
                        done = int(line.split("=")[1]) / 1e6
                    except ValueError:
                        continue
                    last_print = time.time()
                    print(f"PROGRESS {label} {done / max(total, 1) * 100:.0f}% ({time.time() - t0:.0f}s)", flush=True)
            code = p.wait()
            reader.join()
        return code == 0, "".join(err_lines)

    if not has_v:
        ok, err = run([])
        if not ok:
            raise RuntimeError(f"{src.name} 重拼失败：{err[-800:]}")
        return "aac"
    fps = fps_of(st["video"])
    g = str(max(1, round(fps * 2)))
    common = ["-pix_fmt", "yuv420p", "-fps_mode", "passthrough", "-video_track_timescale", "90000", "-g", g]
    hq = quality == "high"
    # 系统 ffmpeg 太新时 NVIDIA 驱动会不支持（报 nvenc API version），依次退到 AMD 核显 AMF、CPU libx264
    tries = [
        ("h264_nvenc", ["-preset", "p5", "-rc", "vbr", "-cq", "18" if hq else "26", "-b:v", "0"]),
        ("h264_amf", ["-quality", "quality", "-rc", "cqp", "-qp_i", "16" if hq else "24",
                      "-qp_p", "18" if hq else "26", "-qp_b", "20" if hq else "28"]),
        ("libx264", ["-preset", "veryfast", "-crf", "17" if hq else "24"]),
    ]
    global _BAD_ENCODERS
    err = ""
    for name, opts in tries:
        if name in _BAD_ENCODERS:
            continue
        ok, err = run(["-c:v", name] + opts + common)
        if ok:
            return name
        _BAD_ENCODERS.add(name)
        print(f"{name} 不可用，换下一个编码器：{err.strip().splitlines()[0][:160] if err.strip() else ''}", flush=True)
    raise RuntimeError(f"{src.name} 重拼失败：{err[-800:]}")


# ---------------------------------------------------------------- 鼠标/按键记录

NUM = r"(-?\d+(?:\.\d+)?)"


def split_objects(text: str) -> tuple[list[str], list[str]]:
    """把 FocuSee 事件文件切成顶层 {...} 对象和它们之间的原文（分隔符、开头、结尾）。

    鼠标记录是一行一个对象，按键记录的对象会跨好几行（activeModifiers 数组），所以按括号配对来切，
    跳过字符串里的括号（按键字符本身可能就是 { 或 }）。返回 (gaps, objs)，len(gaps) == len(objs) + 1。
    """
    gaps, objs = [], []
    depth, start, in_str, esc, last = 0, -1, False, False, 0
    for k, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = k
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0:
                gaps.append(text[last:start])
                objs.append(text[start:k + 1])
                last = k + 1
    gaps.append(text[last:])
    return gaps, objs


def shift_events(src: Path, dst: Path, f: Mapper, t0_ms: float) -> int:
    """改每个事件的 processTimeMs / unixTimeMs（只替换数字，保持 FocuSee 原来的文本格式），再按新时间排序。"""
    text = src.read_bytes().decode("utf-8-sig", errors="replace")  # 不能用 read_text：会把 CRLF 换成 LF
    gaps, objs = split_objects(text)
    out = []
    for o in objs:
        m = re.search(r'"processTimeMs"\s*:\s*' + NUM, o)
        if not m:
            out.append((float("inf"), o))
            continue
        p = float(m.group(1))
        t = (p - t0_ms) / 1000.0
        delta = (f(t) - t) * 1000.0 if 0 < t < f.total else 0.0

        def sub(key, s, d=delta):
            def r(mm):
                v = mm.group(2)
                dec = len(v.split(".")[1]) if "." in v else 0
                return f"{mm.group(1)}{float(v) + d:.{dec}f}"
            return re.sub(rf'("{key}"\s*:\s*){NUM}', r, s, count=1)
        out.append((p + delta, sub("unixTimeMs", sub("processTimeMs", o))))
    out.sort(key=lambda x: x[0])
    parts = [gaps[0]]
    for k, (_, o) in enumerate(out):
        parts += [o, gaps[k + 1]]
    dst.write_bytes("".join(parts).encode("utf-8"))
    return len(out)


# ---------------------------------------------------------------- 工程文件

def map_timed_tracks(tracks: list[dict], f: Mapper, total: float, min_len: float = 0.2) -> list[dict]:
    """begin/end 为 0–1 源比例的轨道（缩放、标注、布局）：换算到新时间，跨块就拆成几段。"""
    out = []
    for t in tracks or []:
        if "begin" not in t or "end" not in t:
            out.append(t)
            continue
        pieces = f.range(float(t["begin"]) * total, float(t["end"]) * total)
        keep = [p for p in pieces if p[1] - p[0] >= min_len] or pieces[:1]
        for a, b in keep:
            nt = copy.deepcopy(t)
            nt["begin"], nt["end"] = a / total, b / total
            out.append(nt)
    out.sort(key=lambda t: t.get("begin", 0))
    return out


def map_subtitle_items(items: list[dict], f: Mapper) -> list[dict]:
    out = []
    for it in items or []:
        pieces = f.range(float(it["start"]), float(it["end"]))
        a, b = max(pieces, key=lambda p: p[1] - p[0])
        ni = dict(it)
        ni["start"], ni["end"] = round(a, 3), round(b, 3)
        out.append(ni)
    out.sort(key=lambda x: x["start"])
    return out


def map_playtracks(tracks: list[dict], f: Mapper, total: float) -> list[dict]:
    keeps = fsproj.playtracks_to_keeps(tracks, total)
    new = []
    for a, b in keeps:
        new += f.range(a, b)
    new = fsproj.merge([p for p in new if p[1] - p[0] > 1e-3], join_gap=1e-3)
    template = (tracks or [None])[0]
    return fsproj.keeps_to_playtracks(new, total, template, min_keep=0.05)


def remap_conf(conf: dict, f: Mapper, total: float, name: str) -> dict:
    c = copy.deepcopy(conf)
    c["playTracks"] = map_playtracks(conf.get("playTracks") or [], f, total)
    for k in ("zoomTracks", "annotationTracks", "layoutTracks"):
        if isinstance(c.get(k), list):
            c[k] = map_timed_tracks(conf[k], f, total)
    if isinstance(c.get("subtitles"), dict) and isinstance(c["subtitles"].get("items"), list):
        c["subtitles"]["items"] = map_subtitle_items(conf["subtitles"]["items"], f)
    if "projectname" in c:
        c["projectname"] = name
    return c


# ---------------------------------------------------------------- 工作目录（逐字稿、剪辑方案）

def remap_transcript(tr: dict, f: Mapper, new_dir: Path) -> tuple[dict, dict]:
    t = copy.deepcopy(tr)
    for s in t["sentences"]:
        for w in s.get("words", []):
            d = w["end"] - w["start"]
            w["start"] = round(f(w["start"]), 3)
            w["end"] = round(w["start"] + d, 3)
            for k in ("asr_start", "asr_end"):
                if k in w:
                    w[k] = round(f(w[k]), 3)
        if s.get("words"):
            s["start"] = min(w["start"] for w in s["words"])
            s["end"] = max(w["end"] for w in s["words"])
        else:
            d = s["end"] - s["start"]
            s["start"] = round(f(s["start"]), 3)
            s["end"] = round(s["start"] + d, 3)
    t["sentences"].sort(key=lambda s: (s["start"], s["id"]))
    idmap = {}
    for i, s in enumerate(t["sentences"]):
        idmap[s["id"]] = i
        s["id"] = i
    t["project"] = str(new_dir)
    if t.get("audio_source"):
        t["audio_source"] = str(new_dir / "recording" / Path(t["audio_source"]).name)
    return t, idmap


def remap_edit(edit: dict, f: Mapper, idmap: dict, new_dir: Path) -> dict:
    e = copy.deepcopy(edit)
    e["project"] = str(new_dir)
    base = []
    for a, b in edit.get("base_keeps") or []:
        base += f.range(a, b)
    e["base_keeps"] = [list(k) for k in fsproj.merge(base, join_gap=1e-3)]
    cuts = []
    for c in edit.get("cuts", []):
        pieces = [p for p in f.range(c["start"], c["end"]) if p[1] - p[0] > 1e-3] or f.range(c["start"], c["end"])[:1]
        for k, (a, b) in enumerate(pieces):
            nc = dict(c)
            nc["start"], nc["end"] = round(a, 3), round(b, 3)
            if k:
                nc["id"] = c["id"] + chr(ord("a") + k - 1)
            if "sentences" in nc:
                nc["sentences"] = sorted(idmap.get(x, x) for x in nc["sentences"])
            cuts.append(nc)
    e["cuts"] = cuts
    e["sentence_text"] = {str(idmap.get(int(k), k)): v for k, v in (edit.get("sentence_text") or {}).items()}
    e["moves"] = []
    e["reordered_from"] = edit.get("project")
    return e


# ---------------------------------------------------------------- 主流程

def unique_name(parent: Path, stem: str) -> Path:
    p = parent / f"{stem}.focusee"
    n = 2
    while p.exists():
        p = parent / f"{stem} {n}.focusee"
        n += 1
    return p


def build(project: str, name: str | None = None, dry_run: bool = False, edit: dict | None = None,
          dest_dir: str | None = None) -> dict:
    pr = fsproj.load(project)
    wd = fsproj.work_dir(pr)
    edit = edit or analyze.load_edit(wd)
    if not edit:
        raise SystemExit("还没有 edit.json")
    moves = [m for m in edit.get("moves") or [] if m.get("enabled", True)]
    if not moves:
        raise SystemExit("没有调换顺序的记录（edit.json → moves 为空）")
    D = pr.duration
    blocks = plan_blocks(moves, D)
    if len(blocks) <= 1:
        raise SystemExit("调换顺序后和原来一样，不需要重拼")
    f = Mapper(blocks, D)
    dst = unique_name(Path(dest_dir) if dest_dir else pr.dir.parent, name or f"{pr.dir.stem} 调序")
    plan = {"source": str(pr.dir), "project": str(dst), "blocks": [[round(a, 3), round(b, 3)] for a, b in blocks],
            "moves": len(moves)}
    if dry_run:
        return plan

    t_start = time.time()
    tmp = dst.with_name(dst.name + ".partial")
    if tmp.exists():
        shutil.rmtree(tmp)
    (tmp / "recording").mkdir(parents=True)
    rec_src, rec_dst = pr.dir / "recording", tmp / "recording"

    # 1. 其他文件原样复制（不带工程备份和美颜前的原始摄像头）
    for item in pr.dir.iterdir():
        if item.name == "recording" or ".bak-" in item.name or item.name.startswith("configure.focuseeproj"):
            continue
        (shutil.copytree if item.is_dir() else shutil.copy2)(item, tmp / item.name)

    # 2. 鼠标/按键记录换算，metadata 换新 uniqueID
    t0_ms = None
    for r in pr.meta.get("recorders", []):
        if r.get("type") in ("legacyDisplay", "display") and r.get("sessions"):
            t0_ms = float(r["sessions"][0].get("processTimeStartMs") or 0)
    event_files = set()
    for r in pr.meta.get("recorders", []):
        if r.get("type") == "input":
            for s in r.get("sessions", []):
                for k in ("mouseClicksFilename", "mouseMovesFilename", "keyStrokesFilename"):
                    if s.get(k):
                        event_files.add(s[k])
                if t0_ms is None:
                    t0_ms = float(s.get("processTimeStartMs") or 0)
    media = []
    for item in rec_src.iterdir():
        if item.is_dir():
            shutil.copytree(item, rec_dst / item.name)
        elif item.name.endswith(SKIP_SUFFIX):
            continue
        elif item.name in event_files:
            n = shift_events(item, rec_dst / item.name, f, t0_ms or 0.0)
            print(f"{item.name}：{n} 条记录已换算", flush=True)
        elif item.name == "metadata.json":
            text = item.read_text("utf-8-sig")
            old = pr.meta.get("uniqueID")
            new_id = str(uuid.uuid4())
            if old and old.isupper():
                new_id = new_id.upper()
            (rec_dst / item.name).write_text(text.replace(old, new_id) if old else text, "utf-8")
        elif item.suffix.lower() in MEDIA_EXT:
            media.append(item)
        else:
            shutil.copy2(item, rec_dst / item.name)

    # 3. 媒体重拼（大文件先做）
    encoders = {}
    for item in sorted(media, key=lambda p: -p.stat().st_size):
        print(f"重拼 {item.name}（{item.stat().st_size / 1e6:.0f} MB）…", flush=True)
        encoders[item.name] = reorder_media(item, rec_dst / item.name, blocks, D, label=item.name)
        got, want = media_duration(rec_dst / item.name), media_duration(item)
        if abs(got - want) > 0.5:
            raise RuntimeError(f"{item.name} 重拼后时长不对：{got:.2f}s，原来 {want:.2f}s")

    # 4. 工程文件
    conf = remap_conf(pr.conf, f, D, dst.stem)
    (tmp / fsproj.PROJ_FILE).write_bytes(fsproj.dumps(conf, pr.newline, pr.trailing_newline).encode("utf-8"))

    tmp.rename(dst)
    print(f"新工程已生成：{dst}", flush=True)

    # 5. 新工程的工作目录：逐字稿、剪辑方案、预览视频跟着换算，审阅页可以直接接着用
    npr = fsproj.load(dst)
    nwd = fsproj.work_dir(npr)
    tr_p = wd / "transcript.json"
    if tr_p.exists():
        tr, idmap = remap_transcript(json.loads(tr_p.read_text("utf-8")), f, dst)
        (nwd / "transcript.json").write_text(json.dumps(tr, ensure_ascii=False, indent=1), "utf-8")
        nedit = remap_edit(edit, f, idmap, dst)
        analyze.save_edit(nwd, nedit)
        try:
            analyze.write_sentences_txt(nwd, tr, nedit)
        except Exception as ex:  # 只是给 agent 看的清单，失败不影响工程
            print(f"sentences.txt 没写成：{ex}", flush=True)
    orig_p = wd / "original.focuseeproj"
    if orig_p.exists():
        oc = json.loads(orig_p.read_text("utf-8-sig"))
        oc["playTracks"] = map_playtracks(oc.get("playTracks") or [], f, D)
        (nwd / "original.focuseeproj").write_text(fsproj.dumps(oc, "\n"), "utf-8")
    (nwd / "project_path.txt").write_text(str(dst), "utf-8")
    for fn in ("screen_terms.txt",):
        if (wd / fn).exists():
            shutil.copy2(wd / fn, nwd / fn)
    if (wd / "beauty.json").exists():
        info = json.loads((wd / "beauty.json").read_text("utf-8"))
        info.update({"inherited_from": str(pr.dir), "webcam": str(dst / "recording" / "webcam-0.mp4"),
                     "original": None})
        (nwd / "beauty.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), "utf-8")
    if (wd / "proxy.mp4").exists():
        print("重拼审阅用预览视频…", flush=True)
        try:
            reorder_media(wd / "proxy.mp4", nwd / "proxy.mp4", blocks, D, label="proxy.mp4", quality="proxy")
        except Exception as ex:
            print(f"预览视频没拼成（可以再跑 make_proxy.py）：{ex}", flush=True)

    plan.update({"seconds": round(time.time() - t_start, 1), "encoders": encoders, "work_dir": str(nwd),
                 "play_tracks": len(conf["playTracks"]),
                 "subtitles": len((conf.get("subtitles") or {}).get("items") or [])})
    return plan


def main():
    fsproj.utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--project")
    ap.add_argument("--name", help="新工程名（默认「原名 调序」，重名自动加序号）")
    ap.add_argument("--dry-run", action="store_true", help="只打印分块方案，不生成")
    ap.add_argument("--edit", help="用这份 edit.json（审阅页里还没保存的状态），不给就用工作目录里的")
    ap.add_argument("--dest-dir", help="新工程放在哪个文件夹（默认和原工程放一起）")
    args = ap.parse_args()
    edit = json.loads(Path(args.edit).read_text("utf-8")) if args.edit else None
    res = build(args.project, args.name, args.dry_run, edit=edit, dest_dir=args.dest_dir)
    print("DONE " + json.dumps(res, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
