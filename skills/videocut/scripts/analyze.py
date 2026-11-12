# /// script
# requires-python = ">=3.10"
# ///
"""根据 transcript.json + 音频静音生成/修改剪辑方案 edit.json。

两层判断（借鉴 screen-studio-editor）：
  1. 程序只依据"音频证据"自动剪：实测静音 > 0.3s 的气口（保留约 0.18s 呼吸）、开头结尾空白、
     没有人声也没有操作的空片段、明确孤立的「嗯/呃」。
  2. 有声音的内容（重录、口误、可能有意义的「那个/就是/然后」）由 agent 读上下文判断，
     每条都写理由；和鼠标点击/按键重叠的语义剪辑默认不启用，必须明确确认。

子命令：
  auto     重新生成自动剪辑（保留已有的 agent/user 剪辑）
  show     刷新 sentences.txt；--sentence N 打印该句逐词时间和词序号
  retake   整句标为重录：--sentences 3,4 或 3-5  --note 理由 [--kept 保留的版本]
  cut      剪一段：--words 12:3-5（第12句第3~5个词）或 --start/--end 秒
  plan     批量执行 agent 计划：--file plan.json（见 SKILL.md）
  accept   启用建议剪辑：--ids c3,c7
  restore  还原（停用）剪辑：--ids c3,c7 或 --sentences 5
  fix      校对某句字幕文字：--sentence 12 --text "正确的文字"（可用 / 指定换行）；--clear 取消
  replace  字幕替换：--from 错字 --to 正字 [--global 记进全局词表，以后所有工程都生效]
  move     调换顺序：--sentences 12-14 --before 3 | --to-end；unmove 取消
  chapter  章节：--sentence N --title 标题（新增，或改这一句上已有章节的标题）；--id ch2 改标题/起始句
  unchapter 删除章节：--ids ch2,ch3；不给 --ids 删除全部
  chapters 打印章节在成片里的时间，并导出 chapters.txt（视频简介用的时间戳）

所有子命令都接受 --project（默认取最新工程）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chapters as chap  # noqa: E402
import fsproj  # noqa: E402

DEFAULT_PARAMS = {
    "pause_threshold": 0.3,  # 实测静音超过这个长度才剪（秒）
    "min_pause": 0.18,       # 剪完保留的气口总长（前多后少，进下一句更利落）
    "silence_db": "auto",    # 静音阈值，auto=按底噪和人声电平自适应；语音被裁就调低（如 -35），停顿残留就调高（如 -25）
    "pause_source": "silence",  # silence=按实测静音；asr=按识别词间距（音频分析失败时自动回退）
    "edge_pad": 0.2,         # 开头第一个词前、结尾最后一个词后保留的余量
    "min_keep": 0.3,         # 短于此长度的保留片段丢弃
    # 几乎总是口头禅的字：孤立出现（前后有停顿或在句首句尾）时自动剪
    "fillers_always": ["嗯", "呃", "额", "唔", "嗯嗯", "呃呃", "um", "uh", "er", "erm", "emm", "em"],
    # 也可能是正常语气助词的：只有前面有停顿（不黏着上一个字）时才剪
    "fillers_loose": ["啊", "哦", "噢", "诶", "欸", "唉", "哎"],
    "loose_gap": 0.15,
    # 可能是口头禅、也可能是正常用词：前后都有停顿时生成"建议"（默认不启用，agent 判断后 accept）
    "fillers_ambiguous": ["那个", "就是说", "这个", "然后", "就是"],
    "ambiguous_gap": 0.25,
    # 字幕
    "max_line_chars": 16,      # 每行最多字数（英文字母按半个字算）
    "target_line_chars": 11,   # 理想行长
    "max_line_seconds": 4.2,   # 一行最长显示时间
    # 写进 FocuSee 的字幕样式。bottomPadding/upPadding 是字幕离画布底/顶边的距离（画布高度的 %），
    # "screen" = 自动放到录屏窗口里、贴着窗口下边缘（按画布比例和窗口位置算）
    "subtitle_style": {"bottomPadding": "screen", "upPadding": "screen", "aligment": 0},
}
OBSOLETE_PARAMS = {"max_pause", "pad"}

PUNCT = "，。！？；：、,.!?;:…—·「」『』“”‘’\"'（）()《》<>【】[] \t"
TURN_WORDS = ("但是", "不过", "然而", "可是", "但")
SPEECH_GUARD = 0.035
BLOCKING_EVENTS = ("click", "key")  # 会拦下语义剪辑的录制事件；滚动不算


def clean(text: str) -> str:
    return "".join(ch for ch in text if ch not in PUNCT).strip().lower()


def global_glossary_path() -> Path:
    return fsproj.data_root() / "glossary.json"


def load_global_glossary() -> list[list[str]]:
    p = global_glossary_path()
    try:
        data = json.loads(p.read_text("utf-8"))
        return [[str(a), str(b)] for a, b in data if a]
    except Exception:
        return []


def hotwords_path() -> Path:
    return global_glossary_path().with_name("hotwords.txt")


def load_hotwords() -> list[str]:
    """所有工程共用的术语表（一行一个），识别时作为热词，agent 校对时作为参考写法。"""
    try:
        lines = hotwords_path().read_text("utf-8").splitlines()
    except OSError:
        return []
    seen, out = set(), []
    for ln in lines:
        t = ln.split("#", 1)[0].strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def hotword_notes() -> list[str]:
    """术语表里带备注的行（`词  # 备注`），给 agent 校对时参考用户习惯。"""
    try:
        lines = hotwords_path().read_text("utf-8").splitlines()
    except OSError:
        return []
    return [ln.strip() for ln in lines if "#" in ln and ln.split("#", 1)[0].strip()]


def add_hotwords(terms) -> list[str]:
    have = {t.lower() for t in load_hotwords()}
    new = []
    for t in terms:
        t = str(t).strip()
        if t and t.lower() not in have and len(t) <= 30:
            have.add(t.lower())
            new.append(t)
    if new:
        hotwords_path().parent.mkdir(parents=True, exist_ok=True)
        with open(hotwords_path(), "a", encoding="utf-8") as f:
            f.write("".join(t + chr(10) for t in new))
    return new


def add_global_replacement(src: str, dst: str) -> None:
    g = load_global_glossary()
    if [src, dst] not in g:
        g.append([src, dst])
    global_glossary_path().parent.mkdir(parents=True, exist_ok=True)
    global_glossary_path().write_text(json.dumps(g, ensure_ascii=False, indent=1), "utf-8")


def load_ctx(project_arg):
    pr = fsproj.load(project_arg)
    wd = fsproj.work_dir(pr)
    fsproj.ensure_original(pr, wd)
    tp = wd / "transcript.json"
    if not tp.exists():
        sys.exit(f"还没有 transcript.json，请先运行 transcribe.py：{tp}")
    transcript = json.loads(tp.read_text("utf-8"))
    return pr, wd, transcript


def load_edit(wd: Path) -> dict | None:
    p = wd / "edit.json"
    return json.loads(p.read_text("utf-8")) if p.exists() else None


def save_edit(wd: Path, edit: dict) -> Path:
    edit["cuts"].sort(key=lambda c: (c["start"], c["end"]))
    p = wd / "edit.json"
    p.write_text(json.dumps(edit, ensure_ascii=False, indent=1), "utf-8")
    return p


def params_of(edit: dict | None) -> dict:
    P = dict(DEFAULT_PARAMS)
    if edit:
        P.update({k: v for k, v in edit.get("params", {}).items() if k not in OBSOLETE_PARAMS})
    if P.get("subtitle_style") == {"upPadding": 2.0, "bottomPadding": 2.0}:  # 旧版默认值（会把字幕压到画布最底下）
        P["subtitle_style"] = dict(DEFAULT_PARAMS["subtitle_style"])
    return P


def next_id(edit: dict) -> str:
    n = 0
    for c in edit["cuts"]:
        m = re.fullmatch(r"c(\d+)", c["id"])
        if m:
            n = max(n, int(m.group(1)))
    return f"c{n + 1}"


def add_cut(edit, start, end, reason, origin, note="", enabled=True, **extra):
    c = {"id": next_id(edit), "start": round(float(start), 3), "end": round(float(end), 3),
         "reason": reason, "origin": origin, "enabled": enabled, "note": note}
    c.update({k: v for k, v in extra.items() if v not in (None, "", [])})
    edit["cuts"].append(c)
    return c


def all_words(transcript):
    out = []
    for s in transcript["sentences"]:
        n = len(s["words"])
        for i, w in enumerate(s["words"]):
            out.append({**w, "sid": s["id"], "wi": i, "first": i == 0, "last": i == n - 1})
    out.sort(key=lambda w: w["start"])
    return out


def is_filler_text(t: str, P: dict) -> bool:
    always = {clean(x) for x in P["fillers_always"]}
    return t in always or (bool(t) and all(ch in "嗯呃额唔" for ch in t))


# ---------------------------------------------------------------- 音频证据

class Evidence:
    """静音区间 + 波形（可选）+ 录制时的操作事件。"""

    def __init__(self, pr, wd: Path, P: dict):
        self.regions: list[tuple[float, float]] = []
        self.wave = None
        self.threshold = None
        self.info: dict = {}
        self.speech_floor = None
        self.events = fsproj.input_events(pr)
        if P.get("pause_source", "silence") == "asr":
            return
        try:
            import audio
            src = fsproj.pick_audio(pr)
            res = audio.analyze_silence(wd, src, P.get("silence_db", "auto"))
            self.regions = [tuple(r) for r in res["regions"]]
            self.threshold = res["threshold_db"]
            self.info = res.get("info", {})
            self.wave = audio.Waveform(wd / "audio.wav")
            noise = self.info.get("noise_p20")
            # 比静音阈值再低一截的"轻声"线：句尾轻读的「吧/嘛/的」常落在这里，剪气口时要保住
            self.speech_floor = max((noise + 12) if noise is not None else -60.0, self.threshold - 14)
        except Exception as e:  # 没 ffmpeg / 音频坏了：回退到按识别词间距
            print(f"⚠️ 音频分析失败，改用识别词间距判断气口：{e}", file=sys.stderr)
            self.regions = []

    @property
    def ok(self) -> bool:
        return bool(self.regions) and self.wave is not None

    def db(self, t: float) -> float:
        return self.wave.db(t)

    def voiced_edges(self, a: float, b: float) -> tuple[float, float] | None:
        """[a,b] 内第一个和最后一个高于轻声线的时间点。"""
        if not self.wave or b <= a:
            return None
        pts = []
        t = a
        while t <= b:
            if self.db(t) > self.speech_floor:
                pts.append(t)
            t += 0.01
        return (pts[0], pts[-1]) if pts else None

    def quiet(self, a: float, b: float, prefer: float | None = None) -> float | None:
        if not self.wave or b < a:
            return None
        return self.wave.quietest(a, b, prefer=prefer)

    def events_in(self, a: float, b: float) -> list[tuple[float, str]]:
        return [e for e in self.events if a <= e[0] <= b]

    def silence_ratio(self, a: float, b: float) -> float:
        if b <= a:
            return 1.0
        ov = sum(max(0.0, min(b, y) - max(a, x)) for x, y in self.regions)
        return ov / (b - a)


def refine_word_times(transcript: dict, ev: Evidence) -> int:
    """识别出的词时间常把词尾拉长到后面的静音里（如「就是」实际 0.2s，识别成 0.7s）。
    按波形把每个词收紧到真正发声的范围（只缩不扩），原始值保存在 asr_start/asr_end。
    这样"这个词有没有被剪掉"的判断和字幕时间都更准。返回修改的词数。"""
    if not ev.wave:
        return 0
    n = 0
    for s in transcript["sentences"]:
        for w in s["words"]:
            a = w.setdefault("asr_start", w["start"])
            b = w.setdefault("asr_end", w["end"])
            edges = ev.voiced_edges(a, b)
            if not edges:
                ns, ne = a, b  # 整个词都在静音里：多半是识别把时间放错了，不动
            else:
                ns, ne = max(a, edges[0] - 0.03), min(b, edges[1] + 0.05)
                if ne - ns < 0.06:
                    ns, ne = a, b
            ns, ne = round(ns, 3), round(ne, 3)
            if (ns, ne) != (w["start"], w["end"]):
                n += 1
            w["start"], w["end"] = ns, ne
        if s["words"]:
            s["start"] = s["words"][0]["start"]
            s["end"] = max(w["end"] for w in s["words"])
    return n


def split_retained(min_pause: float) -> tuple[float, float]:
    """保留的气口前多后少：切完更快进入下一句，不会有"拖尾空白"的感觉。"""
    after = min(0.08, min_pause * 0.33)
    return min_pause - after, after


def protect_words(cs: float, ce: float, words: list[dict], ev: Evidence) -> tuple[float, float]:
    """剪气口时不剪进识别到的词里真正发声的部分（识别时间戳常把词尾拉长到静音里，那部分照剪）。"""
    for w in words:
        if w["end"] <= cs or w["start"] >= ce:
            continue
        a, b = max(cs, w["start"]), min(ce, w["end"])
        edges = ev.voiced_edges(a, b) if ev.wave else (a, b)
        if not edges:
            continue
        if (w["start"] + w["end"]) / 2 <= (cs + ce) / 2:  # 词在切口前半：把切口起点挪到发声之后
            cs = max(cs, edges[1] + 0.06)
        else:                                             # 词在切口后半：把切口终点挪到发声之前
            ce = min(ce, edges[0] - 0.04)
    return cs, ce


def pause_cuts(words: list[dict], duration: float, P: dict, ev: Evidence) -> list[dict]:
    cuts = []
    keep_before, keep_after = split_retained(P["min_pause"])
    min_cut = max(0.12, min(0.2, P["pause_threshold"] - P["min_pause"]))

    def add(a, b, note):
        if b - a >= min_cut:
            cuts.append({"start": round(a, 3), "end": round(b, 3), "reason": "pause", "note": note})

    if words:
        add(0.0, words[0]["start"] - P["edge_pad"], "开头空白")
        add(words[-1]["end"] + P["edge_pad"], duration, "结尾空白")

    if ev.ok:
        for a, b in ev.regions:
            if b - a <= P["pause_threshold"]:
                continue
            cs = 0.0 if a <= 0.05 else a + keep_before
            ce = duration if b >= duration - 0.05 else b - keep_after
            cs, ce = protect_words(cs, ce, words, ev)
            add(cs, ce, f"静音 {b - a:.1f}s")
    else:  # 回退：识别词间距（时间戳有误差，两边多留 0.08s）
        for x, y in zip(words, words[1:]):
            gap = y["start"] - x["end"]
            if gap > P["pause_threshold"]:
                add(x["end"] + keep_before + 0.08, y["start"] - keep_after - 0.08, f"停顿 {gap:.1f}s（按识别）")
    return cuts


def splice_range(words: list[dict], i: int, j: int, P: dict, ev: Evidence) -> tuple[float, float]:
    """剪掉 words[i..j] 时的实际切口：吃掉两侧多余的间隙（剪完只留约 min_pause 的气口），
    切点落在附近波形最安静处，并且不碰到前后的词。"""
    keep_before, keep_after = split_retained(P["min_pause"])
    first, last = words[i], words[j]
    prev = words[i - 1] if i > 0 else None
    nxt = words[j + 1] if j + 1 < len(words) else None
    lo = prev["end"] + SPEECH_GUARD if prev else max(0.0, first["start"] - 0.3)
    hi = nxt["start"] - SPEECH_GUARD if nxt else last["end"] + 0.3
    s_win = (max(lo, first["start"] - 0.3), first["start"])
    e_win = (last["end"], min(hi, last["end"] + 0.3))
    s_pref = min(first["start"], prev["end"] + keep_before) if prev else s_win[0]
    e_pref = max(last["end"], nxt["start"] - keep_after) if nxt else e_win[1]
    s = ev.quiet(*s_win, prefer=s_pref) if s_win[1] >= s_win[0] else None
    e = ev.quiet(*e_win, prefer=e_pref) if e_win[1] >= e_win[0] else None
    s = s_pref if s is None else s
    e = e_pref if e is None else e
    return min(s, first["start"]), max(e, last["end"])


def filler_cuts(words: list[dict], P: dict, ev: Evidence) -> list[dict]:
    cuts = []
    loose = {clean(x) for x in P["fillers_loose"]}
    ambiguous = sorted({clean(x) for x in P["fillers_ambiguous"]}, key=len, reverse=True)
    maxlen = max(map(len, ambiguous), default=0)

    def gap_before(i):
        return words[i]["start"] - words[i - 1]["end"] if i > 0 else 99.0

    def gap_after(j):
        return words[j + 1]["start"] - words[j]["end"] if j + 1 < len(words) else 99.0

    i = 0
    while i < len(words):
        t = clean(words[i]["w"])
        hit = None
        isolated = words[i]["first"] or words[i]["last"] or gap_before(i) >= P["loose_gap"] or gap_after(i) >= P["loose_gap"]
        if is_filler_text(t, P) and isolated:
            hit = (i, i, f"语气词「{t}」", True)
        elif t in loose and (words[i]["first"] or gap_before(i) >= P["loose_gap"]):
            hit = (i, i, f"语气词「{t}」", True)
        else:
            acc = ""
            for j in range(i, min(i + 3, len(words))):
                acc += clean(words[j]["w"])
                if acc in ambiguous and gap_before(i) >= P["ambiguous_gap"] and gap_after(j) >= P["ambiguous_gap"]:
                    hit = (i, j, f"建议：口头禅「{acc}」前后有停顿，可能可以剪", False)
                    break
                if len(acc) >= maxlen:
                    break
        if hit:
            a, b = splice_range(words, hit[0], hit[1], P, ev)
            cuts.append({"start": round(a, 3), "end": round(b, 3), "reason": "filler", "note": hit[2],
                         "enabled": hit[3]})
            i = hit[1] + 1
        else:
            i += 1
    return cuts


def empty_segment_cuts(edit: dict, words: list[dict], duration: float, ev: Evidence) -> list[dict]:
    """没有任何识别词、80% 以上是实测静音、也没有点击/按键的保留片段：整段剪掉。"""
    if not ev.ok:
        return []
    out = []
    for a, b in current_keeps(edit, duration):
        if any(w["end"] > a and w["start"] < b for w in words):
            continue
        if ev.silence_ratio(a, b) >= 0.8 and not ev.events_in(a, b):
            out.append({"start": round(a, 3), "end": round(b, 3), "reason": "pause",
                        "note": f"无人声空片段 {b - a:.1f}s"})
    return out


def current_keeps(edit: dict, duration: float) -> list[tuple[float, float]]:
    enabled = [(c["start"], c["end"]) for c in edit["cuts"] if c["enabled"]]
    base = [tuple(x) for x in edit["base_keeps"]]
    keeps = fsproj.intersect(fsproj.complement(enabled, duration), base)
    return [k for k in keeps if k[1] - k[0] >= params_of(edit)["min_keep"]]


# ---------------------------------------------------------------- 语义剪辑（agent）

def words_text(words: list[dict], a: float, b: float) -> str:
    return "".join(w["w"] + (w.get("p") or "") for w in words if covered(w, [(a, b)]))


def semantic_cut(edit, words, ev: Evidence, P, *, start=None, end=None, span=None, reason="retake",
                 note="", kept="", confidence="high", screen="", origin="agent", sentences=None):
    """span=(i,j) 是 words 下标（含）。切点自动落在安静处；和操作事件重叠时默认不启用。"""
    if span is not None:
        start, end = splice_range(words, span[0], span[1], P, ev)
    if start is None or end is None or end <= start:
        raise ValueError("剪辑范围不合法")
    enabled = confidence in ("high", "medium")
    warn = []
    evs = ev.events_in(start, end)
    # 只有点击和按键算"画面操作"：边讲边滚动页面（读文章、看代码）非常常见，滚动不拦，只在备注里记一笔
    blocking = [e for e in evs if e[1] in BLOCKING_EVENTS]
    scrolls = sum(1 for e in evs if e[1] == "scroll")
    if blocking:
        kinds = {"click": "点击", "key": "按键"}
        desc = "、".join(f"{sum(1 for e in blocking if e[1] == k)} 次{v}" for k, v in kinds.items()
                        if any(e[1] == k for e in blocking))
        if screen != "redundant":
            enabled = False
            warn.append(f"⚠ 这段里有 {desc}，可能会剪掉画面上的操作，所以默认没启用，看过画面确认没问题再启用")
        else:
            warn.append(f"区间内有 {desc}，agent 已确认操作是重复的")
    if scrolls:
        warn.append(f"期间有 {scrolls} 次滚动，剪完画面可能会跳一下")
    if confidence not in ("high", "medium"):
        warn.append("低置信度，仅作建议")
    full_note = "；".join([x for x in [note] + warn if x])
    return add_cut(edit, start, end, reason, origin, full_note, enabled=enabled,
                   removed_text=words_text(words, start, end), kept_text=kept, confidence=confidence,
                   sentences=sentences, activity=len(blocking) or None)


def parse_ids(spec) -> list[int]:
    out = []
    for part in str(spec).replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return sorted(set(out))


def word_span(words, transcript, sid: int, spec: str) -> tuple[int, int]:
    """"3-5" → 第 sid 句第 3~5 个词在全局 words 里的下标。"""
    idx = parse_ids(spec)
    s = next((x for x in transcript["sentences"] if x["id"] == sid), None)
    if not s or not idx or idx[-1] >= len(s["words"]):
        raise ValueError(f"第 {sid} 句没有词 {spec}")
    pos = [k for k, w in enumerate(words) if w["sid"] == sid and w["wi"] in (idx[0], idx[-1])]
    return pos[0], pos[-1]


def sentence_groups(transcript, ids: list[int]):
    ss = [s for s in transcript["sentences"] if s["id"] in ids]
    if not ss:
        raise ValueError(f"没有找到句子 {ids}")
    groups, cur = [], [ss[0]]
    for s in ss[1:]:
        if s["id"] == cur[-1]["id"] + 1:
            cur.append(s)
        else:
            groups.append(cur)
            cur = [s]
    groups.append(cur)
    return groups


def do_decision(edit, d: dict, transcript, words, ev, P) -> list[dict]:
    reason = d.get("reason") or d.get("category") or "retake"
    reason = reason if reason in ("retake", "filler", "manual", "pause") else "retake"
    kw = dict(reason=reason, note=d.get("note") or d.get("why") or "", kept=d.get("kept") or d.get("kept_text") or "",
              confidence=d.get("confidence", "high"), screen=d.get("screen", ""), origin=d.get("origin", "agent"))
    out = []
    if d.get("sentences") is not None:
        for g in sentence_groups(transcript, parse_ids(d["sentences"])):
            i = next(k for k, w in enumerate(words) if w["sid"] == g[0]["id"])
            j = max(k for k, w in enumerate(words) if w["sid"] == g[-1]["id"])
            out.append(semantic_cut(edit, words, ev, P, span=(i, j), sentences=[s["id"] for s in g], **kw))
    elif d.get("sentence") is not None and d.get("words") is not None:
        out.append(semantic_cut(edit, words, ev, P, span=word_span(words, transcript, int(d["sentence"]), d["words"]),
                                sentences=[int(d["sentence"])], **kw))
    elif d.get("start") is not None and d.get("end") is not None:
        out.append(semantic_cut(edit, words, ev, P, start=float(d["start"]), end=float(d["end"]), **kw))
    else:
        raise ValueError(f"无法识别的决策：{d}")
    return out


# ---------------------------------------------------------------- 输出

def fmt_t(t: float) -> str:
    m, s = divmod(max(0.0, t), 60)
    return f"{int(m):02d}:{s:05.2f}"


def covered(w, ranges):
    ln = max(1e-6, w["end"] - w["start"])
    ov = sum(max(0.0, min(w["end"], b) - max(w["start"], a)) for a, b in ranges)
    return ov / ln >= 0.5


def write_sentences_txt(wd: Path, transcript: dict, edit: dict, events=None) -> Path:
    on = [c for c in edit["cuts"] if c["enabled"]]
    ranges = fsproj.merge([(c["start"], c["end"]) for c in on])
    sugg = [c for c in edit["cuts"] if not c["enabled"] and c["origin"] == "auto"]
    fixes = edit.get("sentence_text", {})
    base_cut = fsproj.complement([tuple(x) for x in edit["base_keeps"]], edit["duration"])
    lines = ["# 句子清单：[id] 起-止 文本",
             "# ［］已剪掉的词；〔词?cN〕是建议剪辑（未启用，确认后 accept --ids cN）；⏸ 句前被剪的停顿；🖱 句内的点击/按键数（滚动不计）",
             "# ✎ 后面是已校对的字幕文字；✗ 是你在 FocuSee 里原本就剪掉的句子（不用管）；§ 是章节分界（这一章从下一句开始）"]
    terms = load_hotwords()
    if terms:
        lines.append("# 术语表（标准写法，读音对得上才改成这些）：" + "、".join(terms))
        for note in hotword_notes():
            lines.append("#   " + note)
    st_p = wd / "screen_terms.txt"
    if st_p.exists():
        seen = [ln.split(chr(9))[0] for ln in st_p.read_text("utf-8").splitlines() if ln and not ln.startswith("#")]
        if seen:
            lines.append("# 录屏画面上出现过的词（拼写、大小写以画面为准）：" + "、".join(seen[:60]))
    lines.append("")
    prev_end = None
    events = events or []
    heads = {c["sentence"]: c for c in edit.get("chapters") or []}
    for s in transcript["sentences"]:
        if s["id"] in heads:
            lines.append(f"§ {heads[s['id']]['id']}「{heads[s['id']]['title']}」")
        parts = []
        for w in s["words"]:
            txt = w["w"] + (w.get("p") or "")
            sc = next((c for c in sugg if covered(w, [(c["start"], c["end"])])), None)
            if covered(w, ranges):
                parts.append(f"［{txt}］")
            elif sc:
                parts.append(f"〔{txt}?{sc['id']}〕")
            else:
                parts.append(txt)
        gap = f" ⏸{s['start'] - prev_end:.1f}s" if prev_end is not None and s["start"] - prev_end > 0.6 else ""
        n_ev = sum(1 for t, k in events if s["start"] <= t <= s["end"] and k in BLOCKING_EVENTS)
        ev = f" 🖱{n_ev}" if n_ev else ""
        if s["words"] and all(covered(w, base_cut) for w in s["words"]):
            lines.append(f"[{s['id']}] {fmt_t(s['start'])}-{fmt_t(s['end'])}  ✗（FocuSee 里原本就剪掉了）{s['text']}")
            prev_end = s["end"]
            continue
        lines.append(f"[{s['id']}] {fmt_t(s['start'])}-{fmt_t(s['end'])}{gap}{ev}  {''.join(parts)}")
        if str(s["id"]) in fixes:
            lines.append(f"      ✎ {fixes[str(s['id'])]}")
        prev_end = s["end"]
    p = wd / "sentences.txt"
    p.write_text("\n".join(lines) + "\n", "utf-8")
    return p


def review_warnings(edit: dict, transcript: dict, duration: float) -> list[str]:
    """dry-run 审查清单：长删除、转折词开头的接缝、因操作重叠没启用的剪辑。"""
    words = all_words(transcript)
    warns = []
    for c in edit["cuts"]:
        if c["origin"] == "auto" and c["reason"] == "pause":
            continue
        if c["enabled"] and c["end"] - c["start"] > 5:
            warns.append(f"{c['id']} 删除 {c['end'] - c['start']:.1f}s（{fmt_t(c['start'])}）：{c.get('removed_text', '')[:30]}")
        if c["enabled"] and c["reason"] in ("retake", "manual"):
            nxt = next((w for w in words if w["start"] >= c["end"] - 0.05), None)
            if nxt and clean(nxt["w"]).startswith(TURN_WORDS):
                warns.append(f"{c['id']} 切完后以转折词「{nxt['w']}」开头，检查前后句意是否还连得上")
        if not c["enabled"] and c.get("activity") and c["origin"] == "agent":
            warns.append(f"{c['id']} 与画面操作重叠，未启用：{c.get('note', '')[:40]}")
    if edit.get("chapters"):
        warns += chap.resolve(edit, transcript, duration)[1]
    return warns


# ---------------------------------------------------------------- 调换顺序

def sentence_bounds(transcript: dict, sid: int) -> tuple[float, float]:
    """整句挪动时带走的源时间范围：句子本身 + 前后停顿的一小部分（让接缝自然）。"""
    ss = transcript["sentences"]
    i = next(k for k, s in enumerate(ss) if s["id"] == sid)
    s = ss[i]
    gap_before = s["start"] - ss[i - 1]["end"] if i > 0 else 0.3
    gap_after = ss[i + 1]["start"] - s["end"] if i + 1 < len(ss) else 0.4
    return (round(s["start"] - max(0.0, min(0.12, gap_before / 2)), 3),
            round(s["end"] + max(0.0, min(0.2, gap_after / 2)), 3))


def next_move_id(edit: dict) -> str:
    n = 0
    for m in edit.get("moves", []):
        if str(m.get("id", "")).startswith("m") and m["id"][1:].isdigit():
            n = max(n, int(m["id"][1:]))
    return f"m{n + 1}"


def add_move(edit: dict, transcript: dict, sids: list[int], before_sid: int | None, note: str = "",
             origin: str = "agent") -> dict:
    """把句子 sids（连续的几句）挪到 before_sid 那句前面；before_sid 为 None 表示挪到最后。"""
    a = sentence_bounds(transcript, min(sids))[0]
    b = sentence_bounds(transcript, max(sids))[1]
    before = None if before_sid is None else sentence_bounds(transcript, before_sid)[0]
    if before is not None and a - 0.01 <= before <= b + 0.01:
        raise ValueError("目标位置就在要挪的句子里面，没有变化")
    m = {"id": next_move_id(edit), "start": a, "end": b, "before": before, "sentences": sorted(sids),
         "before_sentence": before_sid, "origin": origin, "enabled": True, "note": note}
    edit.setdefault("moves", []).append(m)
    return m


def summary(edit, duration):
    enabled = [c for c in edit["cuts"] if c["enabled"]]
    base = [tuple(x) for x in edit["base_keeps"]]
    keeps = current_keeps(edit, duration)
    by = {}
    for r in ("pause", "filler", "retake", "manual"):
        cs = [(c["start"], c["end"]) for c in enabled if c["reason"] == r]
        by[r] = {"count": len(cs), "seconds": round(fsproj.total_len(fsproj.intersect(cs, base)), 1)}
    return {"source": round(duration, 1), "before": round(fsproj.total_len(base), 1),
            "after": round(fsproj.total_len(keeps), 1), "segments": len(keeps), "cuts": by,
            "suggestions": sum(1 for c in edit["cuts"] if not c["enabled"] and c["origin"] == "auto"),
            "moves": sum(1 for m in edit.get("moves", []) if m.get("enabled", True)),
            "chapters": len(edit.get("chapters") or [])}


def build_auto(pr, wd, transcript, edit, reset, overrides):
    D = pr.duration
    params = params_of(None if reset else edit)
    for kv in overrides:
        k, v = kv.split("=", 1)
        try:
            params[k] = json.loads(v)
        except json.JSONDecodeError:
            params[k] = v
    kept = [] if (reset or not edit) else [c for c in edit["cuts"] if c["origin"] != "auto"]
    new = {
        "version": 2,
        "project": str(pr.dir),
        "duration": D,
        "params": params,
        "base_keeps": [list(k) for k in fsproj.original_keeps(wd, pr)],
        "cuts": list(kept),
        "replacements": [] if reset else (edit or {}).get("replacements", []),
        "sentence_text": {} if reset else (edit or {}).get("sentence_text", {}),
        "moves": [] if reset else (edit or {}).get("moves", []),
        "chapters": [] if reset else (edit or {}).get("chapters", []),
        "subtitles": (edit or {}).get("subtitles", {"enabled": True}),
    }
    ev = Evidence(pr, wd, params)
    if refine_word_times(transcript, ev):
        (wd / "transcript.json").write_text(json.dumps(transcript, ensure_ascii=False, indent=1), "utf-8")
    words = all_words(transcript)
    for cdef in pause_cuts(words, D, params, ev) + filler_cuts(words, params, ev):
        add_cut(new, cdef["start"], cdef["end"], cdef["reason"], "auto", cdef["note"], enabled=cdef.get("enabled", True))
    for cdef in empty_segment_cuts(new, words, D, ev):
        add_cut(new, cdef["start"], cdef["end"], cdef["reason"], "auto", cdef["note"])
    new["audio"] = {"threshold_db": ev.threshold, **ev.info, "source": "silence" if ev.ok else "asr"}
    return new, ev


def main():
    fsproj.utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("auto")
    a.add_argument("--reset", action="store_true", help="连 agent/user 的剪辑和字幕校对也清掉，从头生成")
    a.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="覆盖参数，如 --set pause_threshold=0.5")
    s = sub.add_parser("show")
    s.add_argument("--sentence", type=int)
    r = sub.add_parser("retake")
    r.add_argument("--sentences", required=True)
    r.add_argument("--note", default="")
    r.add_argument("--kept", default="", help="保留下来的那一遍是哪句/什么内容")
    r.add_argument("--confidence", default="high", choices=["high", "medium", "low"])
    r.add_argument("--screen", default="", choices=["", "redundant"], help="确认区间内的画面操作在保留版本里重复出现")
    c = sub.add_parser("cut")
    c.add_argument("--words", help="句号:词序号，如 12:3-5")
    c.add_argument("--start", type=float)
    c.add_argument("--end", type=float)
    c.add_argument("--reason", default="retake", choices=["manual", "retake", "filler", "pause"])
    c.add_argument("--note", default="")
    c.add_argument("--kept", default="")
    c.add_argument("--confidence", default="high", choices=["high", "medium", "low"])
    c.add_argument("--screen", default="", choices=["", "redundant"])
    c.add_argument("--origin", default="agent", choices=["agent", "user"])
    pl = sub.add_parser("plan")
    pl.add_argument("--file", required=True)
    ac = sub.add_parser("accept")
    ac.add_argument("--ids", required=True)
    rs = sub.add_parser("restore")
    rs.add_argument("--ids")
    rs.add_argument("--sentences")
    fx = sub.add_parser("fix")
    fx.add_argument("--sentence", type=int, required=True)
    fx.add_argument("--text")
    fx.add_argument("--clear", action="store_true")
    mv = sub.add_parser("move", help="调换顺序：把句子挪到另一句前面播放")
    mv.add_argument("--sentences", required=True, help="要挪的句子，单句 12 或连续几句 12-14")
    g = mv.add_mutually_exclusive_group(required=True)
    g.add_argument("--before", type=int, help="挪到这一句前面")
    g.add_argument("--to-end", action="store_true", help="挪到最后")
    mv.add_argument("--note", default="")
    um = sub.add_parser("unmove", help="取消调换顺序")
    um.add_argument("--ids", help="m1,m2；不给就取消全部")
    ch = sub.add_parser("chapter", help="新增/修改章节（由 agent 按语义划分并起标题）")
    ch.add_argument("--sentence", type=int, help="这一章从第几句开始（sentences.txt 的 [id]）")
    ch.add_argument("--title", help="章节标题")
    ch.add_argument("--id", dest="cid", help="要修改的章节，如 ch2；不给就按 --sentence 新增")
    uc = sub.add_parser("unchapter", help="删除章节")
    uc.add_argument("--ids", help="ch2,ch3；不给就删除全部")
    sub.add_parser("chapters", help="打印章节在成片里的时间，并导出 chapters.txt")
    rp = sub.add_parser("replace")
    rp.add_argument("--from", dest="src", required=True)
    rp.add_argument("--to", dest="dst", required=True)
    rp.add_argument("--global", dest="glob", action="store_true", help="写进全局词表，所有工程都生效")
    args = ap.parse_args()

    pr, wd, transcript = load_ctx(args.project)
    D = pr.duration
    edit = load_edit(wd)
    ev = None

    if args.cmd == "auto":
        edit, ev = build_auto(pr, wd, transcript, edit, args.reset, args.set)
    elif edit is None:
        sys.exit("还没有 edit.json，请先运行 analyze.py auto")

    P = params_of(edit)
    words = all_words(transcript)

    def evidence():
        nonlocal ev
        if ev is None:
            ev = Evidence(pr, wd, P)
        return ev

    if args.cmd == "show" and args.sentence is not None:
        s = next((x for x in transcript["sentences"] if x["id"] == args.sentence), None)
        if not s:
            sys.exit(f"没有第 {args.sentence} 句")
        ranges = [(c["start"], c["end"]) for c in edit["cuts"] if c["enabled"]]
        for i, w in enumerate(s["words"]):
            mark = " ✂" if covered(w, ranges) else ""
            print(f"{i:3d} {w['start']:8.3f} {w['end']:8.3f}  {w['w']}{w.get('p', '')}{mark}")
        return
    try:
        if args.cmd == "retake":
            for c in do_decision(edit, {"sentences": args.sentences, "note": args.note, "kept": args.kept,
                                        "confidence": args.confidence, "screen": args.screen},
                                 transcript, words, evidence(), P):
                print(f"{'已标记' if c['enabled'] else '已建议（未启用）'}重录 {c['id']}: 句 {c.get('sentences')} "
                      f"{fmt_t(c['start'])}-{fmt_t(c['end'])} {c['note']}")
        elif args.cmd == "cut":
            d = {"reason": args.reason, "note": args.note, "kept": args.kept, "confidence": args.confidence,
                 "screen": args.screen, "origin": args.origin}
            if args.words:
                sid, spec = args.words.split(":", 1)
                d.update(sentence=int(sid), words=spec)
            else:
                if args.start is None or args.end is None or not 0 <= args.start < args.end <= D + 0.01:
                    sys.exit("需要 --words 或合法的 --start/--end")
                d.update(start=args.start, end=args.end)
            for c in do_decision(edit, d, transcript, words, evidence(), P):
                print(f"{'已添加' if c['enabled'] else '已建议（未启用）'} {c['id']} {fmt_t(c['start'])}-{fmt_t(c['end'])} "
                      f"「{c.get('removed_text', '')}」 {c['note']}")
        elif args.cmd == "plan":
            plan = json.loads(Path(args.file).read_text("utf-8"))
            for d in plan.get("decisions", []):
                if d.get("decision", "cut") != "cut":
                    continue
                for c in do_decision(edit, d, transcript, words, evidence(), P):
                    print(f"{'✂' if c['enabled'] else '?'} {c['id']} {fmt_t(c['start'])}-{fmt_t(c['end'])} "
                          f"「{c.get('removed_text', '')[:40]}」 {c['note'][:60]}")
            for f in plan.get("fixes", []):
                edit.setdefault("sentence_text", {})[str(int(f["sentence"]))] = f["text"]
            if plan.get("fixes"):
                print(f"字幕校对 {len(plan['fixes'])} 句")
            for src, dst in plan.get("replacements", []):
                edit.setdefault("replacements", []).append([src, dst])
            for src, dst in plan.get("global_replacements", []):
                add_global_replacement(src, dst)
            if plan.get("terms"):
                new = add_hotwords(plan["terms"])
                print(f"术语表新增 {len(new)} 个（下次识别作为热词）：{'、'.join(new)}" if new else "术语表没有新增")
            for c in edit["cuts"]:
                if c["id"] in set(plan.get("accept", [])):
                    c["enabled"] = True
            if plan.get("chapters") is not None:
                chap.replace_chapters(edit, plan["chapters"], transcript)
                print(f"章节 {len(edit['chapters'])} 个")
        elif args.cmd == "accept":
            ids = {x.strip() for x in args.ids.split(",") if x.strip()}
            n = 0
            for c in edit["cuts"]:
                if c["id"] in ids and not c["enabled"]:
                    c["enabled"] = True
                    n += 1
            print(f"已启用 {n} 处剪辑")
        elif args.cmd == "restore":
            targets = set()
            if args.ids:
                targets |= {x.strip() for x in args.ids.split(",") if x.strip()}
            if args.sentences:
                ids = parse_ids(args.sentences)
                for s in transcript["sentences"]:
                    if s["id"] in ids:
                        for c in edit["cuts"]:
                            if c["reason"] != "pause" and c["start"] < s["end"] and c["end"] > s["start"]:
                                targets.add(c["id"])
            n = 0
            for c in edit["cuts"]:
                if c["id"] in targets and c["enabled"]:
                    c["enabled"] = False
                    n += 1
            print(f"已还原 {n} 处剪辑")
        elif args.cmd == "fix":
            st = edit.setdefault("sentence_text", {})
            if args.clear:
                st.pop(str(args.sentence), None)
                print(f"已取消第 {args.sentence} 句的校对")
            elif args.text:
                st[str(args.sentence)] = args.text
                print(f"第 {args.sentence} 句字幕 → {args.text}")
            else:
                sys.exit("需要 --text 或 --clear")
        elif args.cmd == "move":
            lo, _, hi = args.sentences.partition("-")
            sids = list(range(int(lo), int(hi or lo) + 1))
            m = add_move(edit, transcript, sids, None if args.to_end else args.before, args.note)
            where = "最后" if args.to_end else f"第 {args.before} 句前面"
            print(f"已调换顺序 {m['id']}：第 {args.sentences} 句 → {where}")
        elif args.cmd == "unmove":
            ids = {x.strip() for x in (args.ids or "").split(",") if x.strip()}
            before = len(edit.get("moves", []))
            edit["moves"] = [m for m in edit.get("moves", []) if ids and m["id"] not in ids]
            print(f"已取消 {before - len(edit['moves'])} 处调换顺序")
        elif args.cmd == "chapter":
            if args.cid is None and (args.sentence is None or not args.title):
                sys.exit("新增章节需要 --sentence 和 --title；修改已有章节用 --id")
            c = chap.set_chapter(edit, args.sentence, args.title, args.cid, transcript)
            print(f"章节 {c['id']}：从第 {c['sentence']} 句开始「{c['title']}」")
        elif args.cmd == "unchapter":
            n = chap.remove_chapters(edit, (args.ids or "").split(","))
            print(f"已删除 {n} 个章节")
        elif args.cmd == "replace":
            if args.glob:
                add_global_replacement(args.src, args.dst)
                print(f"已写入全局词表：{args.src} → {args.dst}")
            else:
                edit.setdefault("replacements", []).append([args.src, args.dst])
                print(f"已添加字幕替换：{args.src} → {args.dst}")
    except ValueError as e:
        sys.exit(str(e))

    p = save_edit(wd, edit)
    st = write_sentences_txt(wd, transcript, edit, fsproj.input_events(pr))
    out = {"edit": str(p), "sentences_txt": str(st), "summary": summary(edit, D)}
    exp = chap.write_export(wd, edit, transcript, D)
    if exp:
        out["chapters_txt"] = exp["path"]
        if args.cmd in ("chapter", "unchapter", "chapters", "plan"):
            for c in exp["chapters"]:
                print(f"{chap.fmt_ts(c['start'], exp['total'] >= 3600)} {c['title']}  "
                      f"[{c['id']} 第 {c['start_sentence']} 句起，{c['end'] - c['start']:.0f} 秒]")
            for n in chap.notes(exp["chapters"]):
                print("提示：" + n)
    elif args.cmd == "chapters":
        print("还没有章节：用 chapter --sentence N --title 标题 添加，或在 plan.json 里写 chapters")
    if args.cmd == "auto":
        out["audio"] = edit.get("audio")
    w = review_warnings(edit, transcript, D)
    if w:
        out["review"] = w
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
