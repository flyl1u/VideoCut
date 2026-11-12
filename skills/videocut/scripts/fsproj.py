# /// script
# requires-python = ">=3.10"
# ///
"""FocuSee 工程读写核心库（只用标准库）。

工程结构（trackVersion 2.3.0.3 实测）：
  <name>.focusee/
    configure.focuseeproj   JSON，所有编辑信息
    recording/metadata.json 录制元数据
    recording/display-0.mp4, microphone-0.m4a, webcam-0.mp4, systemaudio-0.m4a ...

剪辑 = playTracks：[{speed, type, isDisplayCursor, begin, end, duration}]
begin/end 是 0~1 的源视频比例，duration 是源时长（秒）。段之间的空隙就是被剪掉的部分。
zoomTracks / annotationTracks / subtitles 都用源时间，所以剪辑时只改 playTracks。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

PROJ_FILE = "configure.focuseeproj"
DEFAULT_PROJECTS_DIR = Path(r"D:\FocuSee\FocuSee Projects")
MIN_KEEP = 0.3  # 短于这个长度的保留片段直接丢掉（秒）

Range = tuple[float, float]


def utf8_stdout() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass


# ---------------------------------------------------------------- 区间运算

def merge(ranges: list[Range], join_gap: float = 0.0) -> list[Range]:
    """合并重叠（或间距 <= join_gap）的区间，返回排好序的列表。"""
    out: list[list[float]] = []
    for a, b in sorted((float(a), float(b)) for a, b in ranges if b > a):
        if out and a <= out[-1][1] + join_gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def complement(cuts: list[Range], total: float) -> list[Range]:
    keeps, t = [], 0.0
    for a, b in merge(cuts):
        a, b = max(0.0, a), min(total, b)
        if a > t:
            keeps.append((t, a))
        t = max(t, b)
    if t < total:
        keeps.append((t, total))
    return keeps


def intersect(xs: list[Range], ys: list[Range]) -> list[Range]:
    xs, ys = merge(xs), merge(ys)
    out, i, j = [], 0, 0
    while i < len(xs) and j < len(ys):
        a, b = max(xs[i][0], ys[j][0]), min(xs[i][1], ys[j][1])
        if b > a:
            out.append((a, b))
        if xs[i][1] < ys[j][1]:
            i += 1
        else:
            j += 1
    return out


def total_len(ranges: list[Range]) -> float:
    return sum(b - a for a, b in merge(ranges))


# ---------------------------------------------------------------- 工程

@dataclass
class Project:
    dir: Path
    conf: dict
    meta: dict
    newline: str
    trailing_newline: bool

    @property
    def name(self) -> str:
        return self.conf.get("projectname") or self.dir.stem

    @property
    def duration(self) -> float:
        """源时长（秒）。优先用 playTracks 里记录的 duration。"""
        for t in self.conf.get("playTracks") or []:
            if t.get("duration"):
                return float(t["duration"])
        for r in self.meta.get("recorders", []):
            if r.get("type") in ("legacyDisplay", "display"):
                return float(r["sessions"][0]["durationMs"]) / 1000.0
        raise ValueError("无法确定源视频时长")

    @property
    def unique_id(self) -> str:
        return self.meta.get("uniqueID") or self.dir.stem

    @property
    def conf_path(self) -> Path:
        return self.dir / PROJ_FILE


def resolve_project(path: str | os.PathLike | None) -> Path:
    """接受 .focusee 文件夹或其中的 configure.focuseeproj；为空时取最新修改的工程。"""
    if not path:
        cands = [p for p in DEFAULT_PROJECTS_DIR.glob("*.focusee") if (p / PROJ_FILE).exists()]
        if not cands:
            raise FileNotFoundError(f"{DEFAULT_PROJECTS_DIR} 下没有找到 .focusee 工程")
        return max(cands, key=lambda p: (p / PROJ_FILE).stat().st_mtime)
    p = Path(path)
    if p.is_file() and p.name == PROJ_FILE:
        p = p.parent
    if not (p / PROJ_FILE).exists():
        raise FileNotFoundError(f"不是 FocuSee 工程（缺少 {PROJ_FILE}）：{p}")
    return p


def load(path: str | os.PathLike | None) -> Project:
    d = resolve_project(path).resolve()
    raw = (d / PROJ_FILE).read_bytes()
    text = raw.decode("utf-8-sig")
    meta_p = d / "recording" / "metadata.json"
    meta = json.loads(meta_p.read_text("utf-8-sig")) if meta_p.exists() else {}
    return Project(
        dir=d,
        conf=json.loads(text),
        meta=meta,
        newline="\r\n" if "\r\n" in text else "\n",
        trailing_newline=text.endswith("\n"),
    )


def dumps(conf: dict, newline: str = "\r\n", trailing_newline: bool = False) -> str:
    s = json.dumps(conf, ensure_ascii=False, indent=2)
    if newline != "\n":
        s = s.replace("\n", newline)
    if trailing_newline:
        s += newline
    return s


DATA_DIR_NAME = "videocut"
OLD_DATA_DIR_NAMES = ("focusee-cut",)  # 改名前用过的目录，第一次运行时整个搬过来


def data_root() -> Path:
    """%LOCALAPPDATA%/videocut：术语表、热词、各工程的工作目录都在这里。"""
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    d = base / DATA_DIR_NAME
    if not d.exists():
        for old in OLD_DATA_DIR_NAMES:
            if (base / old).is_dir():
                try:
                    os.rename(base / old, d)
                except OSError:
                    pass
                break
    d.mkdir(parents=True, exist_ok=True)
    return d


def work_dir(project: Project) -> Path:
    # 按工程路径区分（复制出来的测试副本 uniqueID 相同，不能只用 uniqueID）
    key = hashlib.sha1(str(project.dir.resolve()).lower().encode("utf-8")).hexdigest()[:8]
    safe = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in project.dir.stem).strip()
    d = data_root() / f"{safe}-{key}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_original(project: Project, wd: Path) -> Path:
    """第一次处理时把工程文件原样存一份，之后所有"原始保留区间"都以它为准。"""
    orig = wd / "original.focuseeproj"
    if not orig.exists():
        shutil.copy2(project.conf_path, orig)
        (wd / "project_path.txt").write_text(str(project.dir), "utf-8")
    return orig


def original_keeps(wd: Path, project: Project) -> list[Range]:
    orig = wd / "original.focuseeproj"
    conf = json.loads(orig.read_text("utf-8-sig")) if orig.exists() else project.conf
    return playtracks_to_keeps(conf.get("playTracks") or [], project.duration)


def playtracks_to_keeps(tracks: list[dict], total: float) -> list[Range]:
    if not tracks:
        return [(0.0, total)]
    return merge([(float(t["begin"]) * total, float(t["end"]) * total) for t in tracks])


def order_keeps(keeps: list[Range], moves: list[dict] | None, total: float) -> list[Range]:
    """按「调换顺序」记录重排保留片段，返回播放顺序的片段列表（不再按时间排序）。

    每条 move：{start, end, before}——把源时间 [start, end] 里保留的部分挪到源时间 before 的位置前面播放
    （before 为 None 或 ≥ 总长表示放到最后）。按列表顺序依次执行；before 落在剪掉的地方时，
    放到它之后第一个片段前面。
    """
    seq = [tuple(k) for k in merge(keeps)]
    for m in moves or []:
        if m.get("enabled") is False:
            continue
        s, e, b = float(m["start"]), float(m["end"]), m.get("before")
        moved, rest = [], []
        for a, z in seq:
            if z <= s or a >= e:
                rest.append((a, z))
                continue
            if a < s:
                rest.append((a, s))
            moved.append((max(a, s), min(z, e)))
            if z > e:
                rest.append((e, z))
        if not moved:
            continue
        idx = None
        if b is None or float(b) >= total:
            idx = len(rest)
        else:
            b = float(b)
            for i, (a, z) in enumerate(rest):
                if a < b < z:
                    rest[i:i + 1] = [(a, b), (b, z)]
                    idx = i + 1
                    break
                if abs(a - b) < 1e-9:
                    idx = i
                    break
            if idx is None:
                idx = next((i for i, (a, _) in enumerate(rest) if a >= b), len(rest))
        seq = rest[:idx] + moved + rest[idx:]
    out: list[Range] = []
    for a, z in seq:  # 播放顺序里首尾相接的片段合并回去
        if out and abs(out[-1][1] - a) < 1e-6:
            out[-1] = (out[-1][0], z)
        else:
            out.append((a, z))
    return out


def keeps_to_playtracks(keeps: list[Range], total: float, template: dict | None = None,
                        min_keep: float = MIN_KEEP, ordered: bool = False) -> list[dict]:
    """ordered=True：keeps 已经是播放顺序（调换过顺序），不再排序合并，也不丢掉拆分出来的短片段。"""
    tpl = {"speed": 1.0, "type": 0, "isDisplayCursor": True}
    if template:
        tpl.update({k: v for k, v in template.items() if k not in ("begin", "end", "duration")})
    out = []
    for a, b in (keeps if ordered else merge(keeps)):
        a, b = max(0.0, a), min(total, b)
        if b - a < (0.02 if ordered else min_keep):
            continue
        t = copy.deepcopy(tpl)
        t["begin"] = round(a / total, 12)
        t["end"] = 1.0 if b >= total - 1e-6 else round(b / total, 12)
        t["duration"] = total
        out.append(t)
    if not out:
        raise ValueError("剪辑后没有剩下任何片段，拒绝写入")
    return out


def focusee_running() -> list[str]:
    """返回**有窗口的** FocuSee 进程（Windows），也就是编辑器/录制界面开着。

    FocuSee 关掉窗口后常驻托盘，进程还在但没有主窗口，这时工程已经保存并释放，可以安全写入。
    """
    ps = ("Get-Process | Where-Object { $_.ProcessName -like 'FocuSee*' } | "
          "ForEach-Object { $_.ProcessName + '|' + $_.MainWindowHandle + '|' + $_.MainWindowTitle }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=20).stdout
    except Exception:
        return _focusee_processes()
    names = set()
    for line in out.splitlines():
        parts = line.strip().split("|", 2)
        if len(parts) >= 2 and parts[1].strip() not in ("", "0"):
            title = parts[2].strip() if len(parts) > 2 else ""
            names.add(parts[0] + (f"（{title}）" if title else ""))
    return sorted(names)


def _focusee_processes() -> list[str]:
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                             text=True, encoding="mbcs", errors="replace", timeout=15).stdout
    except Exception:
        return []
    names = set()
    for line in out.splitlines():
        name = line.split('","')[0].strip('"')
        if name.lower().startswith("focusee"):
            names.add(name)
    return sorted(names)


# ---------------------------------------------------------------- 画面几何

def display_size(project: Project) -> tuple[float, float]:
    for r in project.meta.get("recorders", []):
        if r.get("type") in ("legacyDisplay", "display") and r.get("sessions"):
            b = r["sessions"][0].get("bounds") or {}
            if b.get("width") and b.get("height"):
                return float(b["width"]), float(b["height"])
    return 16.0, 9.0


def screen_bottom_margin(project: Project) -> float:
    """录屏窗口下边缘离画布底边的距离，单位是画布高度的百分比（和字幕 bottomPadding 同一单位）。

    实测 4:3 画布上 bottomPadding=2 时字幕底边离画布底边 1.9% 画布高，所以 bottomPadding 就是这个百分比。
    """
    import re
    bg = project.conf.get("background") or {}
    w, h = display_size(project)
    rect = ((bg.get("crop") or {}).get("rect")) or {}
    a_s = (w * float(rect.get("width") or 1)) / (h * float(rect.get("height") or 1))
    m = re.fullmatch(r"ratio_(\d+)_(\d+)", str(bg.get("ratio") or ""))
    a_c = int(m.group(1)) / int(m.group(2)) if m else a_s  # 原始尺寸/自动：画布和屏幕同比例
    inset = float(bg.get("padding") or 0) * min(a_c, 1.0)  # 画布高 = 1，宽 = a_c
    iw, ih = a_c - 2 * inset, 1 - 2 * inset
    sh = ih if iw / ih > a_s else iw / a_s
    return max(0.0, (1 - sh) / 2 * 100)


def save(project: Project, conf: dict, backup: bool = True) -> Path | None:
    bak = None
    if backup:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        bak = project.conf_path.with_name(f"{PROJ_FILE}.bak-{stamp}")
        shutil.copy2(project.conf_path, bak)
    data = dumps(conf, project.newline, project.trailing_newline).encode("utf-8")
    tmp = project.conf_path.with_suffix(".tmp-videocut")
    tmp.write_bytes(data)
    os.replace(tmp, project.conf_path)
    project.conf = conf
    return bak


# ---------------------------------------------------------------- 媒体

def ffbin(name: str) -> str:
    env = os.environ.get(name.upper())
    if env:
        return env
    p = shutil.which(name)
    if not p:
        raise FileNotFoundError(f"找不到 {name}，请先安装 ffmpeg 并加入 PATH")
    return p


def has_audio(path: Path) -> bool:
    if not path.exists():
        return False
    r = subprocess.run([ffbin("ffprobe"), "-v", "error", "-select_streams", "a",
                        "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    return bool(r.stdout.strip())


AUDIO_PRIORITY = ["microphone-0.m4a", "webcam-0.mp4", "systemaudio-0.m4a", "display-0.mp4"]


def pick_audio(project: Project) -> Path:
    rec = project.dir / "recording"
    for name in AUDIO_PRIORITY:
        p = rec / name
        if has_audio(p):
            return p
    raise FileNotFoundError(f"{rec} 里没有找到带音频的文件（{', '.join(AUDIO_PRIORITY)}）")


def display_video(project: Project) -> Path:
    rec = project.dir / "recording"
    for r in project.meta.get("recorders", []):
        if r.get("type") in ("legacyDisplay", "display"):
            fn = r.get("sessions", [{}])[0].get("outputFilename")
            if fn and (rec / fn).exists():
                return rec / fn
    p = rec / "display-0.mp4"
    if p.exists():
        return p
    raise FileNotFoundError(f"找不到屏幕录像：{rec}")


def _load_lenient_json(path: Path):
    """FocuSee 的事件文件常带尾逗号（`[..., ]`），标准 json 解析不了。"""
    import re
    text = path.read_text("utf-8-sig", errors="replace")
    return json.loads(re.sub(r",\s*([\]}])", r"\1", text))


def input_events(project: Project) -> list[tuple[float, str]]:
    """录制时的鼠标点击/滚轮/按键事件，换算成源时间（秒）。读不到就返回空列表。

    语义剪辑（重录、口误）如果和这些操作重叠，可能会剪掉教程里的关键操作，需要 agent 明确确认。
    """
    t0 = None
    for r in project.meta.get("recorders", []):
        if r.get("type") in ("legacyDisplay", "display") and r.get("sessions"):
            t0 = float(r["sessions"][0].get("processTimeStartMs") or 0)
            break
    events: list[tuple[float, str]] = []
    rec = project.dir / "recording"
    for r in project.meta.get("recorders", []):
        if r.get("type") != "input":
            continue
        for s in r.get("sessions", []):
            base = t0 if t0 is not None else float(s.get("processTimeStartMs") or 0)
            for key, kinds in (("mouseClicksFilename", {"mouseDown": "click"}),
                               ("keyStrokesFilename", {"keyDown": "key"})):
                fn = s.get(key)
                if not fn or not (rec / fn).exists():
                    continue
                try:
                    data = _load_lenient_json(rec / fn)
                except Exception:
                    continue
                for e in data if isinstance(data, list) else []:
                    if not isinstance(e, dict) or e.get("isARepeat"):
                        continue
                    kind = kinds.get(e.get("type"))
                    if kind is None and e.get("button") == "wheel":
                        kind = "scroll"
                    if kind and e.get("processTimeMs") is not None:
                        events.append((round((float(e["processTimeMs"]) - base) / 1000.0, 3), kind))
    events.sort()
    return events


if __name__ == "__main__":
    utf8_stdout()
    pr = load(sys.argv[1] if len(sys.argv) > 1 else None)
    keeps = playtracks_to_keeps(pr.conf.get("playTracks") or [], pr.duration)
    print(json.dumps({
        "project": str(pr.dir), "name": pr.name, "duration": pr.duration,
        "segments": len(keeps), "kept": round(total_len(keeps), 3),
        "audio": str(pick_audio(pr)), "work_dir": str(work_dir(pr)),
        "focusee_running": focusee_running(),
    }, ensure_ascii=False, indent=2))
