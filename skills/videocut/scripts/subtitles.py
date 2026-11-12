# /// script
# requires-python = ">=3.10"
# ///
"""字幕生成：词级时间戳 → 适合观看的短行。

- 只保留剪辑后还在的词，去掉语气词；所有时长/停顿按"剪后时间"计算（观众看到的节奏）。
- agent 校对过的整句文字（edit.json 的 sentence_text）和替换词表会先套用，再按字对齐回原来的词时间；
  校对文字里的 "/" 表示这里必须换行；用了 "/" 的句子，每段放得下就整段一行，不再自动拆。
- 断句用动态规划在整段里找总代价最小的分法：优先在句号/逗号/停顿处断，行长接近理想长度，
  不让「的/了/吗」开头，不在介词/连词后面断，不把英文单词拆开，单行不超过 max_line_chars。
"""
from __future__ import annotations

import bisect
import difflib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze  # noqa: E402
import fsproj  # noqa: E402

SOFT = set("，,、；;：:")
HARD = set("。！？!?…")
ALL_PUNCT = SOFT | HARD | set(".—·「」『』“”‘’\"'（）()《》<>【】[]")
WEAK_START = set("的了着过们吗呢吧啊嘛呀")
WEAK_START_OK = ("的确", "了解", "了不起", "着重", "着急", "着手", "过程", "过去", "过来", "过于", "过度", "呢子")
BAD_END = set("在把被和跟对给从让与及或而向往比为")  # 单字介词/连词放行尾很别扭
# 修饰后面内容的词放行尾也别扭（"开发了一个/非常简单的网页"、"这三个/部分"）
BAD_END_WORDS = ("个", "非常", "很", "最", "更", "比较", "一些", "这些", "那些", "所有", "有点", "特别", "一下",
                 "可以", "能够", "需要", "应该", "就是")
WEAK_START_LOC = ("中", "里", "上面", "下面", "里面")  # 方位词黏着前面的名词
WEAK_START_LOC_OK = ("中国", "中间", "中心", "中文", "里面的话", "上面我们", "下面我们")
HARD_GAP = 1.0  # 剪后停顿超过这个就一定换行


def vlen(text: str) -> float:
    n = 0.0
    for ch in text:
        if ch.isascii():
            n += 0.3 if ch == " " else 0.55
        else:
            n += 1.0
    return n


def is_ascii_word(ch: str) -> bool:
    return bool(ch) and ch.isascii() and ch.isalnum()


WORD_INNER = set(".'")  # 夹在英文字母/数字中间时属于单词本身：Next.js、GPT-5.5、don't
WORDISH = set(".'+#-_")  # 英文词里可能出现的符号：C++、C#、gpt-4o


def is_inner(text: str, i: int) -> bool:
    return text[i] in WORD_INNER and 0 < i < len(text) - 1         and is_ascii_word(text[i - 1]) and is_ascii_word(text[i + 1])


def strip_punct(text: str) -> str:
    """去掉标点，但保留英文词内部的点和撇号。"""
    return "".join(ch for i, ch in enumerate(text) if ch not in ALL_PUNCT or is_inner(text, i)).strip()


def is_wordish(ch: str) -> bool:
    return bool(ch) and ch.isascii() and (ch.isalnum() or ch in WORDISH)


def join_tokens(tokens: list[dict]) -> str:
    """拼一行：中文直接相连；句中标点变成空格；两个英文单词之间加空格（逐字母识别的 A/P/P 不加）。"""
    out = ""
    for k, tok in enumerate(tokens):
        t = tok["t"]
        if k:
            prev = tokens[k - 1]
            if prev["p"]:
                out += " "
            elif is_ascii_word(prev["t"][-1:]) and is_ascii_word(t[:1]) and (len(prev["t"]) > 1 or len(t) > 1):
                out += " "
        out += t
    return re.sub(r"\s+", " ", out).strip()


def plain_sentence(words: list[dict]) -> str:
    s = ""
    for k, w in enumerate(words):
        if k and is_ascii_word(words[k - 1]["w"][-1:]) and is_ascii_word(w["w"][:1]) \
                and (len(words[k - 1]["w"]) > 1 or len(w["w"]) > 1):
            s += " "
        s += w["w"] + (w.get("p") or "")
    return s


def compile_replacements(pairs: list) -> list[tuple[re.Pattern, str]]:
    out = []
    for src, dst in pairs:
        parts = [re.escape(ch) for ch in str(src) if not ch.isspace()]
        if not parts:
            continue
        pat = r"\s*".join(parts)
        if str(src).strip().isascii():  # 英文词要整词匹配：js 不改 json，也不改 Next.js / Node.js 里的 js
            pat = rf"(?<![A-Za-z0-9])(?<![A-Za-z0-9][.']){pat}(?![A-Za-z0-9])(?![.'][A-Za-z0-9])"
        out.append((re.compile(pat, re.IGNORECASE), str(dst)))
    return out


def align(words: list[dict], text: str) -> list[dict]:
    """把校对后的文字按字对齐回原词，返回带时间的 token 列表。"""
    orig = []  # (char, word_index)
    for wi, w in enumerate(words):
        for ch in w["w"]:
            if ch not in ALL_PUNCT and not ch.isspace():
                orig.append((ch, wi))
    new = []  # [char, punct_after, brk_after]
    for i, ch in enumerate(text):
        if is_inner(text, i):
            new.append([ch, "", False])
        elif ch == "/":
            if new:
                new[-1][2] = True
        elif ch in ALL_PUNCT:
            if new:
                new[-1][1] += ch
        elif ch.isspace():
            if new and is_ascii_word(new[-1][0]):
                new[-1][1] += " "  # 英文词间空格，作为词边界
        else:
            new.append([ch, "", False])
    if not new:
        return []
    a = "".join(c for c, _ in orig).lower()
    b = "".join(c[0] for c in new).lower()
    owner = [None] * len(new)
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            for k in range(j2 - j1):
                owner[j1 + k] = orig[i1 + k][1]
        elif tag == "replace":
            for k in range(j2 - j1):
                owner[j1 + k] = orig[i1 + min(i2 - i1 - 1, k * (i2 - i1) // (j2 - j1))][1]
    # 插入的字跟前一个字走（开头的跟后一个字走）
    last = None
    for k in range(len(new)):
        if owner[k] is None:
            owner[k] = last
        last = owner[k]
    nxt = None
    for k in range(len(new) - 1, -1, -1):
        if owner[k] is None:
            owner[k] = nxt
        nxt = owner[k]
    if owner[0] is None:
        owner = [0] * len(new)

    groups: list[list[int]] = []
    for k in range(len(new)):
        prev = groups[-1][-1] if groups else None
        joined = prev is not None and not new[prev][1] and not new[prev][2]
        same_word = joined and owner[prev] == owner[k]
        # 替换后连在一起的英文（JavaScript ← java + spring）保持一个词，不拆开也不加空格
        ascii_run = joined and is_wordish(new[prev][0]) and is_wordish(new[k][0])
        if same_word or ascii_run:
            groups[-1].append(k)
        else:
            groups.append([k])
    # 一个原词被拆成多个 token 时按字数分时间
    by_word: dict[int, list[int]] = {}
    for gi, g in enumerate(groups):
        by_word.setdefault(owner[g[0]], []).append(gi)
    toks = []
    for gi, g in enumerate(groups):
        w = words[owner[g[0]]]
        w_last = words[owner[g[-1]]]
        if w_last is not w:  # 跨了多个原词
            start, end = w["start"], w_last["end"]
        else:
            sibs = by_word[owner[g[0]]]
            total = sum(len(groups[x]) for x in sibs)
            before = sum(len(groups[x]) for x in sibs if x < gi)
            span = w["end"] - w["start"]
            start = w["start"] + span * before / total
            end = w["start"] + span * (before + len(g)) / total
        toks.append({
            "t": "".join(new[k][0] for k in g),
            "p": new[g[-1]][1].strip(),
            "brk": new[g[-1]][2],
            "start": start,
            "end": end,
            "word": w,
        })
    return toks


def edited_mapper(keeps: list[tuple[float, float]]):
    keeps = fsproj.merge(keeps)
    starts = [a for a, _ in keeps]
    cum, acc = [], 0.0
    for a, b in keeps:
        cum.append(acc)
        acc += b - a

    def f(t: float) -> float:
        i = bisect.bisect_right(starts, t) - 1
        if i < 0:
            return 0.0
        a, b = keeps[i]
        return cum[i] + min(t, b) - a
    return f


def is_filler(text: str, P: dict) -> bool:
    t = analyze.clean(text)
    if not t:
        return False
    loose = {analyze.clean(x) for x in P["fillers_loose"]}
    return analyze.is_filler_text(t, P) or t in loose


def sentence_tokens(s: dict, P: dict, reps, overrides: dict) -> list[dict]:
    words = s["words"]
    if not words:
        return []
    base = plain_sentence(words)
    text = overrides.get(str(s["id"]))
    custom = text is not None
    if text is None:
        text = base
    for pat, dst in reps:
        text = pat.sub(dst, text)
    if text == base and not custom:
        toks = []
        for w in words:
            core = strip_punct(w["w"])
            if not core or is_filler(core, P):
                if toks and w.get("p"):
                    toks[-1]["p"] += w["p"]
                continue
            toks.append({"t": core, "p": (w.get("p") or "").strip(), "brk": False,
                         "start": w["start"], "end": w["end"], "word": w, "sid": s["id"]})
        return toks
    toks = align(words, text)
    if not custom:  # 只是套用了替换词表：语气词照样去掉
        toks = [t for t in toks if not is_filler(t["t"], P)]
    manual = custom and "/" in text
    for t in toks:
        t["sid"] = s["id"]
        t["manual"] = manual
    if manual and toks:  # agent 用 / 手动分行的句子：句尾也断开，不和下一句拼在一起
        toks[-1]["brk"] = True
    return toks


def doc_tokens(transcript: dict, params: dict, replacements: list, sentence_text: dict) -> list[dict]:
    """审阅页「逐字稿」用：每句的显示文字和逐字时间。校对过/套用替换词的句子按新文字对齐，语气词也保留（方便看和还原）。"""
    P = {**analyze.DEFAULT_PARAMS, **params}
    reps = compile_replacements(list(analyze.load_global_glossary()) + list(replacements or []))
    overrides = sentence_text or {}
    out = []
    for s in transcript["sentences"]:
        words = s["words"]
        if not words:
            continue
        base = plain_sentence(words)
        fix = overrides.get(str(s["id"]))
        text = fix if fix is not None else base
        for pat, dst in reps:
            text = pat.sub(dst, text)
        if fix is None and text == base:
            toks = [{"t": strip_punct(w["w"]) or w["w"], "p": (w.get("p") or "").strip(),
                     "start": w["start"], "end": w["end"]} for w in words]
        else:
            toks = [{"t": t["t"], "p": t["p"],
                     "start": t["start"], "end": t["end"]} for t in align(words, text)]
        out.append({"id": s["id"], "start": s["start"], "end": s["end"], "text": text, "fixed": fix is not None,
                    "tokens": [{**t, "start": round(t["start"], 3), "end": round(t["end"], 3)} for t in toks]})
    return out


def boundary_cost(toks, j, ed) -> float:
    """在 toks[j] 后面断开的代价（越小越好）。"""
    tok, nxt = toks[j], toks[j + 1]
    p = tok["p"]
    gap = ed(nxt["start"]) - ed(tok["end"])
    c = 0.0
    if any(ch in HARD for ch in p):
        c -= 22
    elif any(ch in SOFT for ch in p):
        c -= 12
    else:
        c += 14
    if gap >= 0.35:
        c -= 10
    elif gap >= 0.2:
        c -= 5
    if not p:
        if nxt["t"][:1] in WEAK_START and not nxt["t"].startswith(WEAK_START_OK):
            c += 40
        if tok["t"][-1:] in BAD_END and len(tok["t"]) == 1:
            c += 14
        if tok["t"].endswith(BAD_END_WORDS):
            c += 12
        if nxt["t"].startswith(WEAK_START_LOC) and not nxt["t"].startswith(WEAK_START_LOC_OK):
            c += 16
        if is_ascii_word(tok["t"][-1:]) and is_ascii_word(nxt["t"][:1]) and gap < 0.22:
            c += 200
        if len(tok["t"]) == 1 and not tok["t"].isascii():
            c += 6
    return c


def line_cost(toks, i, j, ed, P) -> float | None:
    text = join_tokens(toks[i:j + 1])
    L = vlen(text)
    maxc, target = float(P["max_line_chars"]), float(P["target_line_chars"])
    if L > maxc and j > i:
        return None
    dur = ed(toks[j]["end"]) - ed(toks[i]["start"])
    c = 5.0  # 每多一行的固定代价，避免切得太碎
    c += (target - L) * 0.9 if L < target else (L - target) * 1.6
    c += max(0.0, dur - float(P["max_line_seconds"])) * 15
    if dur < 0.7:
        c += 6
    if L < 4 and not (i == 0 and j == len(toks) - 1):
        c += 14  # 「所以」「然后我」这种碎行单独一行很难看，宁可并进前后行
    for k in range(i, j):
        if any(ch in HARD for ch in toks[k]["p"]):
            c += 9  # 一行里塞了两句话
    return c


def split_block(toks, ed, P) -> list[list[dict]]:
    n = len(toks)
    # agent 用 / 指定好的一段：放得下就整段一行，不再自动拆
    if all(t.get("manual") for t in toks) and vlen(join_tokens(toks)) <= float(P["max_line_chars"]):
        return [toks]
    best = [0.0] + [float("inf")] * n
    back = [0] * (n + 1)
    for j in range(n):
        for i in range(j, -1, -1):
            lc = line_cost(toks, i, j, ed, P)
            if lc is None:
                break
            if best[i] == float("inf"):
                continue
            bc = boundary_cost(toks, j, ed) if j + 1 < n else 0.0
            v = best[i] + lc + bc
            if v < best[j + 1]:
                best[j + 1], back[j + 1] = v, i
    lines, j = [], n
    while j > 0:
        i = back[j]
        lines.append(toks[i:j])
        j = i
    return lines[::-1]


def build(transcript: dict, keeps: list, params: dict, replacements: list, sentence_text: dict) -> list[dict]:
    P = {**analyze.DEFAULT_PARAMS, **params}
    ed = edited_mapper(keeps)
    cut_ranges = fsproj.complement(keeps, float(transcript.get("duration") or 1e9))
    reps = compile_replacements(list(analyze.load_global_glossary()) + list(replacements or []))
    stream: list[dict] = []
    for s in transcript["sentences"]:
        for t in sentence_tokens(s, P, reps, sentence_text or {}):
            if analyze.covered(t["word"], cut_ranges):
                continue
            stream.append(t)
    stream.sort(key=lambda t: t["start"])

    blocks, cur = [], []
    for t in stream:
        prev = cur[-1] if cur else None
        manual_edge = prev is not None and (bool(t.get("manual")) != bool(prev.get("manual"))
                                            or (t.get("manual") and t.get("sid") != prev.get("sid")))
        if prev and (ed(t["start"]) - ed(prev["end"]) > HARD_GAP or prev["brk"] or manual_edge):
            blocks.append(cur)
            cur = []
        cur.append(t)
    if cur:
        blocks.append(cur)

    lines = []
    for b in blocks:
        for ln in split_block(b, ed, P):
            text = join_tokens(ln)
            if text:
                lines.append({"start": ln[0]["start"], "end": ln[-1]["end"], "text": text})
    lines.sort(key=lambda x: x["start"])
    for a, b in zip(lines, lines[1:]):  # 剪后间隔很短就补齐，避免字幕闪烁
        if ed(b["start"]) - ed(a["end"]) < 0.5:
            a["end"] = max(a["end"], b["start"])
    return [{"start": round(x["start"], 3), "end": round(x["end"], 3), "text": x["text"]} for x in lines]
