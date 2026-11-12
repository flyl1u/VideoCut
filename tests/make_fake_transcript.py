"""用工程里 FocuSee 自带的句级字幕伪造一份词级 transcript.json（没有 API key 时测试流程用）。

每句的字均匀分配时间；再在部分较长停顿里插入「嗯」，句首偶尔插入「那个」，用来测试语气词规则。
用法：uv run --no-project python tests/make_fake_transcript.py "<xxx.focusee>"
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".claude" / "skills" / "videocut" / "scripts"))
import fsproj  # noqa: E402

fsproj.utf8_stdout()
pr = fsproj.load(sys.argv[1])
wd = fsproj.work_dir(pr)
fsproj.ensure_original(pr, wd)
items = pr.conf.get("subtitles", {}).get("items") or []
if not items:
    sys.exit("这个工程没有 FocuSee 字幕，无法伪造")

sentences = []
for k, it in enumerate(items):
    text = it["text"].replace(" ", "，")
    chars = [c for c in text]
    a, b = float(it["start"]), float(it["end"])
    # 句子两头各留一点静音，模拟真实停顿
    a2, b2 = a + min(0.25, (b - a) * 0.1), b - min(0.35, (b - a) * 0.15)
    step = (b2 - a2) / max(1, len(chars))
    words = []
    for i, ch in enumerate(chars):
        if ch == "，":
            if words:
                words[-1]["p"] = "，"
            continue
        words.append({"w": ch, "p": "", "start": round(a2 + i * step, 3), "end": round(a2 + (i + 1) * step, 3)})
    if k % 9 == 4 and len(words) > 4:  # 句首插「那个」，前后留停顿
        t0 = words[0]["start"]
        for w in words:
            w["start"] = round(w["start"] + 0.0, 3)
        words = [{"w": "那", "p": "", "start": round(t0 - 0.0, 3), "end": round(t0 + 0.15, 3)},
                 {"w": "个", "p": "", "start": round(t0 + 0.15, 3), "end": round(t0 + 0.3, 3)}] + \
                [{**w, "start": round(w["start"] + 0.6, 3), "end": round(min(b, w["end"] + 0.6), 3)} for w in words]
    if words:
        words[-1]["p"] = words[-1]["p"] or "。"
        sentences.append({"start": words[0]["start"], "end": words[-1]["end"],
                          "text": "".join(w["w"] + w["p"] for w in words), "words": words})

# 在一部分句间停顿里插入「嗯」
for k in range(len(sentences) - 1):
    s, n = sentences[k], sentences[k + 1]
    if k % 5 == 2 and n["start"] - s["end"] > 0.1:
        m = s["end"] + 0.12
        s["words"].append({"w": "嗯", "p": "", "start": round(m, 3), "end": round(m + 0.3, 3)})
        s["end"] = round(m + 0.3, 3)

for i, s in enumerate(sentences):
    s["id"] = i
doc = {"version": 1, "engine": "fake-from-focusee-subtitles", "project": str(pr.dir),
       "audio_source": "fake", "audio_sha1": "fake", "duration": pr.duration, "sentences": sentences}
(wd / "transcript.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), "utf-8")
print(f"已写入 {wd / 'transcript.json'}：{len(sentences)} 句，{sum(len(s['words']) for s in sentences)} 词")
