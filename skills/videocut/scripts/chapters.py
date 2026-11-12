# /// script
# requires-python = ">=3.10"
# ///
"""章节：按语义给成片分段并起标题，导出视频平台简介里用的时间戳列表。

存储：edit.json → chapters = [{"id": "ch1", "sentence": 句子id, "title": "标题"}, ...]
  - 章节锚定在「从哪一句开始」（源逐字稿的句子 id），不存时间：剪辑一改，时间自动跟着重算。
  - 由 agent 读 sentences.txt 按语义划分并起标题（analyze.py chapter / plan.json 的 chapters），
    用户在审阅页里改标题、拖动分界、增删。

时间换算：成片时间 = 按播放顺序（有调换顺序 moves 时按新顺序）把保留片段首尾相接后的时间。
  - 章节起点 = 锚定句第一处保留内容在成片里的时间；锚定句整句被剪掉时顺延到后面第一句有保留内容的句子。
  - 第一章总是从 00:00 开始（片头的空白、开场白都算进第一章；YouTube 也要求第一个时间戳是 00:00）。

FocuSee 工程里没有章节/标记字段（annotationTracks 是叠在画面上的文字、箭头、马赛克，会被导出进视频），
所以章节只存在 edit.json，并导出成工作目录里的 chapters.txt，由用户贴进视频简介。
"""
from __future__ import annotations

import bisect
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fsproj  # noqa: E402

MIN_CHAPTER_SECONDS = 10.0  # YouTube：每章至少 10 秒，至少 3 章，第一章从 00:00 开始才会识别成章节
MIN_OVERLAP = 0.05          # 句子和保留片段重叠超过这么多秒才算"这句有保留内容"
EXPORT_NAME = "chapters.txt"


# ---------------------------------------------------------------- 编辑 edit.json → chapters

def chapter_list(edit: dict) -> list[dict]:
    return edit.setdefault("chapters", [])


def next_chapter_id(edit: dict) -> str:
    n = 0
    for c in edit.get("chapters") or []:
        m = re.fullmatch(r"ch(\d+)", str(c.get("id", "")))
        if m:
            n = max(n, int(m.group(1)))
    return f"ch{n + 1}"


def _check_sentence(transcript: dict | None, sid: int) -> None:
    if transcript is not None and not any(s["id"] == sid for s in transcript["sentences"]):
        raise ValueError(f"没有第 {sid} 句")


def _clean_title(title) -> str:
    t = " ".join(str(title or "").split())
    if not t:
        raise ValueError("章节标题不能为空")
    return t


def set_chapter(edit: dict, sentence: int | None = None, title: str | None = None, cid: str | None = None,
                transcript: dict | None = None, origin: str = "agent") -> dict:
    """新增或修改章节。
    给 cid：改这一章的标题和/或起始句；不给 cid：这一句已经是某章起点就改它的标题，否则新增一章。"""
    chs = chapter_list(edit)
    if sentence is not None:
        sentence = int(sentence)
        _check_sentence(transcript, sentence)
    if cid:
        c = next((x for x in chs if x["id"] == cid), None)
        if c is None:
            raise ValueError(f"没有章节 {cid}")
        if sentence is not None:
            other = next((x for x in chs if x["sentence"] == sentence and x is not c), None)
            if other:
                raise ValueError(f"第 {sentence} 句已经是章节 {other['id']}「{other['title']}」的起点")
            c["sentence"] = sentence
        if title is not None:
            c["title"] = _clean_title(title)
        return c
    if sentence is None:
        raise ValueError("新增章节需要 --sentence")
    c = next((x for x in chs if x["sentence"] == sentence), None)
    if c:
        if title is not None:
            c["title"] = _clean_title(title)
        return c
    c = {"id": next_chapter_id(edit), "sentence": sentence, "title": _clean_title(title), "origin": origin}
    chs.append(c)
    chs.sort(key=lambda x: x["sentence"])
    return c


def replace_chapters(edit: dict, items: list[dict], transcript: dict | None = None, origin: str = "agent") -> list[dict]:
    """整体替换章节（plan.json 的 chapters）：[{"sentence": 0, "title": "开场"}, ...]。"""
    edit["chapters"] = []
    for it in items:
        set_chapter(edit, int(it["sentence"]), it.get("title"), transcript=transcript, origin=origin)
    return edit["chapters"]


def remove_chapters(edit: dict, ids=None) -> int:
    """删除章节；ids 为空表示全部删除。返回删掉的个数。"""
    chs = edit.get("chapters") or []
    ids = {x.strip() for x in (ids or []) if str(x).strip()}
    keep = [c for c in chs if ids and c["id"] not in ids]
    edit["chapters"] = keep
    return len(chs) - len(keep)


# ---------------------------------------------------------------- 时间换算

def playback_sequence(edit: dict, duration: float) -> list[tuple[float, float]]:
    """播放顺序的保留片段（源时间）。和 apply.py 写进 playTracks 的保留片段一致，有 moves 时按新顺序。"""
    import analyze  # 延迟导入：analyze 也会导入本模块
    keeps = analyze.current_keeps(edit, duration)
    moves = [m for m in edit.get("moves") or [] if m.get("enabled", True)]
    return fsproj.order_keeps(keeps, moves, duration) if moves else [tuple(k) for k in keeps]


def sentence_out_starts(transcript: dict, seq: list[tuple[float, float]]) -> dict[int, float]:
    """每句第一处保留内容在成片里的时间（整句被剪掉的句子不在结果里）。"""
    segs, acc = [], 0.0
    for a, b in seq:
        segs.append((a, b, acc))
        acc += b - a
    segs.sort()  # 按源时间排，方便二分（片段之间互不重叠）
    ends = [b for _, b, _ in segs]
    out: dict[int, float] = {}
    for s in transcript["sentences"]:
        s0, s1 = float(s["start"]), float(s["end"])
        need = min(MIN_OVERLAP, (s1 - s0) / 2)
        best = None
        k = bisect.bisect_right(ends, s0)
        while k < len(segs) and segs[k][0] < s1:
            a, b, c = segs[k]
            if min(b, s1) - max(a, s0) >= max(need, 1e-6):
                t = c + max(a, s0) - a
                best = t if best is None else min(best, t)
            k += 1
        if best is not None:
            out[s["id"]] = best
    return out


def resolve(edit: dict, transcript: dict, duration: float) -> tuple[list[dict], list[str], float]:
    """算出每章在成片里的起止时间。返回 (chapters, warnings, 成片总长)。

    chapters 按成片时间排序，每项：id, title, sentence（存的起始句）, start_sentence（实际起始句，
    锚定句被剪掉时顺延）, start, end, source_start（源时间，审阅页跳转用）。
    """
    seq = playback_sequence(edit, duration)
    total = sum(b - a for a, b in seq)
    outs = sentence_out_starts(transcript, seq)
    by_id = {s["id"]: s for s in transcript["sentences"]}
    order = [s["id"] for s in sorted(transcript["sentences"], key=lambda s: (s["start"], s["id"]))]
    warns: list[str] = []
    found = []
    for c in edit.get("chapters") or []:
        sid = c.get("sentence")
        if sid not in by_id:
            warns.append(f"章节 {c.get('id')}「{c.get('title')}」的起始句 {sid} 不存在（逐字稿重新识别过？），已忽略")
            continue
        real = sid
        if real not in outs:  # 起始句整句被剪掉（常见：分章时选了重录前的那一遍）：顺延，不算问题
            k = order.index(sid)
            real = next((x for x in order[k + 1:] if x in outs), None)
            if real is None:
                warns.append(f"章节 {c['id']}「{c['title']}」从第 {sid} 句往后全被剪掉了，已忽略")
                continue
        found.append({"id": c["id"], "title": c["title"], "sentence": sid, "start_sentence": real,
                      "start": outs[real], "source_start": float(by_id[real]["start"])})
    found.sort(key=lambda x: (x["start"], x["sentence"]))
    out: list[dict] = []
    for ch in found:
        if out and abs(ch["start"] - out[-1]["start"]) < 1e-6:
            warns.append(f"章节 {ch['id']}「{ch['title']}」和 {out[-1]['id']}「{out[-1]['title']}」起点相同，已忽略")
            continue
        out.append(ch)
    if out:
        out[0]["content_start"] = out[0]["start"]
        out[0]["start"] = 0.0
    for i, ch in enumerate(out):
        ch["end"] = out[i + 1]["start"] if i + 1 < len(out) else total
        ch["start"], ch["end"] = round(ch["start"], 3), round(ch["end"], 3)
        if ch["end"] - ch["start"] < MIN_CHAPTER_SECONDS:
            warns.append(f"章节 {ch['id']}「{ch['title']}」只有 {ch['end'] - ch['start']:.1f} 秒"
                         f"（YouTube 要求每章至少 {MIN_CHAPTER_SECONDS:.0f} 秒）")
    return out, warns, round(total, 3)


# ---------------------------------------------------------------- 导出

def fmt_ts(t: float, hours: bool = False) -> str:
    """成片时间 → 视频平台的时间戳：00:00 / 1:02:03（向下取整到秒，跳过去不会漏掉章节开头）。"""
    s = int(max(0.0, t) + 1e-6)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if hours else f"{m:02d}:{s:02d}"


def export_text(chapters: list[dict], total: float) -> str:
    """一行一章：「00:00 开场」，总长超过 1 小时用 H:MM:SS。可以直接贴进 B 站 / YouTube 简介。"""
    hours = total >= 3600
    return "".join(f"{fmt_ts(c['start'], hours)} {c['title']}\n" for c in chapters)


def write_export(wd: Path, edit: dict, transcript: dict, duration: float) -> dict | None:
    """把章节时间戳写到工作目录的 chapters.txt。没有章节时删掉旧文件并返回 None。"""
    p = Path(wd) / EXPORT_NAME
    if not edit.get("chapters"):
        if p.exists():
            p.unlink()
        return None
    chs, warns, total = resolve(edit, transcript, duration)
    text = export_text(chs, total)
    p.write_text(text, "utf-8")
    return {"path": str(p), "text": text, "chapters": chs, "warnings": warns, "total": total}


def notes(chapters: list[dict]) -> list[str]:
    """给 agent 的提示（不算问题）：平台识别章节的条件。"""
    out = []
    if 0 < len(chapters) < 3:
        out.append("只有 %d 章：YouTube 至少 3 章才会显示成章节进度条（B 站简介里的时间戳不受影响）" % len(chapters))
    return out
