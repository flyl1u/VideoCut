# /// script
# requires-python = ">=3.10"
# dependencies = ["dashscope>=1.24,<3", "requests"]
# ///
"""抽取工程音频 → 阿里百炼 fun-asr 转写 → transcript.json（词级时间戳，源时间，保留语气词）。

用法：
  uv run transcribe.py --project "<xxx.focusee>" [--force] [--language zh]

需要环境变量 DASHSCOPE_API_KEY（音频会上传到阿里云百炼临时存储）。
同一份音频按哈希缓存，重复运行不会重复计费；--force 强制重新识别。
术语表 %LOCALAPPDATA%/videocut/hotwords.txt 和全局词表里的目标写法会作为热词提交，提高专有名词识别率；
热词服务出错时只提示，不影响识别。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fsproj  # noqa: E402

MODEL = "fun-asr"
PUNCT_CHARS = "，。！？；：、,.!?;:…—·“”‘’\"'（）()《》"


def extract_audio(src: Path, dst: Path) -> None:
    cmd = [fsproj.ffbin("ffmpeg"), "-y", "-v", "error", "-i", str(src), "-vn",
           "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "64k", str(dst)]
    subprocess.run(cmd, check=True)


def sha1(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def get_api_key() -> str:
    key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not key and sys.platform == "win32":
        # 用户用 setx 设置后，当前进程可能还没继承到，直接读注册表里的用户环境变量
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                key = str(winreg.QueryValueEx(k, "DASHSCOPE_API_KEY")[0]).strip()
        except OSError:
            key = ""
    if not key:
        sys.exit("缺少 DASHSCOPE_API_KEY。请在百炼控制台创建 API Key，然后在 PowerShell 里运行：\n"
                 "  setx DASHSCOPE_API_KEY \"你的key\"\n并重开终端/会话。")
    return key


def _get(obj, key):
    """dashscope 的返回对象有时是 dict，有时是属性对象。"""
    try:
        return obj[key]
    except (TypeError, KeyError):
        return getattr(obj, key, None)


VOCAB_PREFIX = "videocut"  # 百炼要求：小写字母和数字，少于 10 个字符


def hotword_list() -> list[str]:
    import analyze
    terms = analyze.load_hotwords() + [dst for _, dst in analyze.load_global_glossary()]
    out, seen = [], set()
    for t in terms:
        t = t.strip()
        # 百炼热词：每个不超过 15 个汉字/单词，最多 500 个
        if t and t.lower() not in seen and len(t) <= 15:
            seen.add(t.lower())
            out.append(t)
    return out[:500]


def vocabulary_id(key: str) -> str | None:
    """按术语表创建/更新百炼热词表，返回 vocabulary_id；失败返回 None（不影响识别）。"""
    terms = hotword_list()
    if not terms:
        return None
    import analyze
    cache_p = analyze.global_glossary_path().with_name("vocabulary.json")
    digest = hashlib.sha1(chr(10).join(terms).encode("utf-8")).hexdigest()
    try:
        cache = json.loads(cache_p.read_text("utf-8"))
    except Exception:
        cache = {}
    if cache.get("sha1") == digest and cache.get("id"):
        return cache["id"]
    vocab = [{"text": t, "weight": 4, "lang": "en" if t.isascii() else "zh"} for t in terms]
    try:
        from dashscope.audio.asr import VocabularyService
        svc = VocabularyService(api_key=key)
        if cache.get("id"):
            svc.update_vocabulary(cache["id"], vocab)
            vid = cache["id"]
        else:
            vid = svc.create_vocabulary(prefix=VOCAB_PREFIX, target_model=MODEL, vocabulary=vocab)
    except Exception as e:  # noqa: BLE001
        print(f"⚠ 热词表没用上（{type(e).__name__}: {e}），继续普通识别", file=sys.stderr)
        return None
    cache_p.write_text(json.dumps({"id": vid, "sha1": digest, "count": len(terms)}, ensure_ascii=False), "utf-8")
    return vid


def transcribe(audio: Path, language: str | None) -> dict:
    import dashscope
    import requests
    from dashscope.audio.asr import Transcription
    from dashscope.utils.oss_utils import OssUtils

    key = get_api_key()
    dashscope.api_key = key
    print(f"上传音频到百炼临时存储：{audio.name} ({audio.stat().st_size / 1e6:.1f} MB)", file=sys.stderr)
    url, _ = OssUtils.upload(model=MODEL, file_path=str(audio), api_key=key)

    kwargs = {"disfluency_removal_enabled": False}  # 保留 嗯/啊 等语气词，后面要靠它们剪
    if language:
        kwargs["language_hints"] = [language]
    vid = vocabulary_id(key)
    if vid:
        kwargs["vocabulary_id"] = vid
        print(f"使用术语热词 {len(hotword_list())} 个", file=sys.stderr)
    task = Transcription.async_call(model=MODEL, file_urls=[url], api_key=key,
                                    headers={"X-DashScope-OssResourceResolve": "enable"}, **kwargs)
    if task.status_code != 200:
        sys.exit(f"提交转写任务失败：{task.status_code} {task.code} {task.message}")
    task_id = _get(task.output, "task_id")
    print(f"转写任务已提交：{task_id}，等待结果…", file=sys.stderr)
    t0 = time.time()
    res = Transcription.wait(task=task_id, api_key=key)
    if res.status_code != 200 or _get(res.output, "task_status") != "SUCCEEDED":
        sys.exit(f"转写失败：{res.status_code} {getattr(res, 'message', '')} {json.dumps(res.output, ensure_ascii=False, default=str)}")
    item = dict(_get(res.output, "results")[0])
    if item.get("subtask_status") not in (None, "SUCCEEDED"):
        sys.exit(f"转写子任务失败：{json.dumps(item, ensure_ascii=False)}")
    raw = requests.get(item["transcription_url"], timeout=60).json()
    print(f"转写完成，用时 {time.time() - t0:.0f}s", file=sys.stderr)
    return raw


def normalize(raw: dict) -> list[dict]:
    """把 fun-asr 的结果转成 [{id,start,end,text,words:[{w,p,start,end}]}]，时间单位秒。"""
    sentences = []
    for tr in raw.get("transcripts", []):
        for s in tr.get("sentences", []):
            words = []
            for w in s.get("words") or []:
                # fun-asr 的标点有时在 text 末尾（"好，"），有时单独成一个带时长的词（"："），统一挂到前一个词的 p 上
                text = (w.get("text") or "").strip()
                punct = w.get("punctuation", "") or ""
                core = text.rstrip(PUNCT_CHARS)
                punct = text[len(core):] + punct
                if not core:
                    if words:
                        words[-1]["p"] += punct
                    continue
                words.append({
                    "w": core,
                    "p": punct,
                    "start": round(w["begin_time"] / 1000.0, 3),
                    "end": round(w["end_time"] / 1000.0, 3),
                })
            if not words and s.get("text"):
                words = [{"w": s["text"], "p": "", "start": round(s["begin_time"] / 1000.0, 3),
                          "end": round(s["end_time"] / 1000.0, 3)}]
            sentences.append({
                "start": round(s["begin_time"] / 1000.0, 3),
                "end": round(s["end_time"] / 1000.0, 3),
                "text": s.get("text", ""),
                "words": words,
            })
    sentences.sort(key=lambda s: s["start"])
    for i, s in enumerate(sentences):
        s["id"] = i
    return sentences


def main() -> None:
    fsproj.utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", help=".focusee 工程文件夹（默认取最新的工程）")
    ap.add_argument("--language", default="zh", help="语种提示，传空字符串表示自动")
    ap.add_argument("--force", action="store_true", help="忽略缓存重新识别")
    args = ap.parse_args()

    pr = fsproj.load(args.project)
    wd = fsproj.work_dir(pr)
    fsproj.ensure_original(pr, wd)
    src = fsproj.pick_audio(pr)
    audio = wd / "audio.mp3"
    print(f"工程：{pr.dir}\n音频来源：{src.name}", file=sys.stderr)
    extract_audio(src, audio)
    digest = sha1(audio)

    out = wd / "transcript.json"
    if out.exists() and not args.force:
        old = json.loads(out.read_text("utf-8"))
        if old.get("audio_sha1") == digest:
            print(json.dumps({"transcript": str(out), "cached": True,
                              "sentences": len(old["sentences"])}, ensure_ascii=False))
            return

    raw = transcribe(audio, args.language or None)
    (wd / "asr_raw.json").write_text(json.dumps(raw, ensure_ascii=False, indent=1), "utf-8")
    sentences = normalize(raw)
    doc = {
        "version": 1,
        "engine": MODEL,
        "project": str(pr.dir),
        "audio_source": src.name,
        "audio_sha1": digest,
        "duration": pr.duration,
        "sentences": sentences,
    }
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), "utf-8")
    nwords = sum(len(s["words"]) for s in sentences)
    print(json.dumps({"transcript": str(out), "cached": False, "sentences": len(sentences),
                      "words": nwords}, ensure_ascii=False))


if __name__ == "__main__":
    main()
