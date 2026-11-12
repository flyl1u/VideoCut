# /// script
# requires-python = ">=3.10"
# ///
"""音频证据：真实静音区间 + 波形最安静的剪切点（只用标准库 + ffmpeg）。

思路借鉴 screen-studio-editor：ASR 的词时间戳会漂几十到几百毫秒，
所以"哪里没声音"以实测静音为准，"剪在哪一帧"以波形能量最低点为准；
ASR 只用来说明剪掉的是什么、保护识别到的词。
"""
from __future__ import annotations

import array
import json
import math
import subprocess
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fsproj  # noqa: E402

AUTO_FLOOR_DB = -30.5   # 自动阈值下限：防止零星峰值把几乎无声的片段算成有声
AUTO_CEIL_DB = -18.0


def ensure_wav(wd: Path, src: Path) -> Path:
    """16k 单声道 16bit PCM，按源文件修改时间缓存。"""
    wav = wd / "audio.wav"
    stamp = wd / "audio.wav.src"
    sig = f"{src}|{src.stat().st_mtime_ns}|{src.stat().st_size}"
    if wav.exists() and stamp.exists() and stamp.read_text("utf-8") == sig:
        return wav
    subprocess.run([fsproj.ffbin("ffmpeg"), "-y", "-v", "error", "-i", str(src), "-vn",
                    "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)], check=True)
    stamp.write_text(sig, "utf-8")
    return wav


class Waveform:
    def __init__(self, path: Path):
        with wave.open(str(path), "rb") as h:
            if h.getnchannels() != 1 or h.getsampwidth() != 2:
                raise RuntimeError("需要单声道 16bit PCM WAV")
            self.rate = h.getframerate()
            self.samples = array.array("h")
            self.samples.frombytes(h.readframes(h.getnframes()))
        if sys.byteorder != "little":
            self.samples.byteswap()
        self._env = None

    FRAME = 0.01

    def envelope(self) -> array.array:
        """每 10ms 一帧的均方能量（只算一次）。"""
        if self._env is None:
            n = int(self.rate * self.FRAME)
            s = self.samples
            env = array.array("d")
            for i in range(0, len(s), n):
                seg = s[i:i + n]
                env.append(sum(x * x for x in seg) / max(1, len(seg)))
            self._env = env
        return self._env

    def db(self, t: float) -> float:
        """t 附近 20ms 的电平（dBFS）。"""
        env = self.envelope()
        i = int(t / self.FRAME)
        a, b = max(0, i - 1), min(len(env), i + 1)
        if b <= a:
            return -120.0
        ms = sum(env[a:b]) / (b - a)
        return 10 * math.log10(max(ms, 1.0) / (32768.0 ** 2))

    def rms(self, t: float, window: float = 0.024) -> float:
        c = int(t * self.rate)
        r = max(1, int(window * self.rate / 2))
        a, b = max(0, c - r), min(len(self.samples), c + r)
        if b <= a:
            return float("inf")
        seg = self.samples[a:b]
        return math.sqrt(sum(x * x for x in seg) / (b - a))

    def quietest(self, a: float, b: float, step: float = 0.008, prefer: float | None = None) -> float | None:
        """[a, b] 里能量最低的时间点；能量相同时取离 prefer（默认中点）最近的。"""
        if b < a:
            return None
        prefer = (a + b) / 2 if prefer is None else prefer
        best = None
        t = a
        while t <= b + 1e-9:
            cand = (round(self.rms(t), 1), abs(t - prefer), t)
            if best is None or cand < best:
                best = cand
            t += step
        return best[2] if best else None

    def percentiles(self) -> tuple[float, float] | None:
        """短窗能量的 p20（底噪）和 p80（人声）dBFS。"""
        win = max(1, int(self.rate * 0.025))
        step = max(win, int(self.rate * 0.12))
        levels = []
        for s in range(0, max(0, len(self.samples) - win), step):
            seg = self.samples[s:s + win]
            v = math.sqrt(sum(x * x for x in seg) / len(seg))
            levels.append(20 * math.log10(max(v, 1.0) / 32768.0))
        if not levels:
            return None
        levels.sort()
        pick = lambda q: levels[min(len(levels) - 1, int((len(levels) - 1) * q))]
        return pick(0.2), pick(0.8)


def resolve_threshold(wf: Waveform, requested) -> tuple[float, dict]:
    if requested not in (None, "", "auto"):
        return float(requested), {"mode": "fixed"}
    p = wf.percentiles()
    if not p or p[1] <= p[0]:
        return -28.0, {"mode": "fallback"}
    noise, speech = p
    thr = max(AUTO_FLOOR_DB, min(AUTO_CEIL_DB, round(noise + 0.45 * (speech - noise), 1)))
    return thr, {"mode": "auto", "noise_p20": round(noise, 1), "speech_p80": round(speech, 1)}


def detect_silence(wav: Path, noise_db: float, min_dur: float = 0.25) -> list[tuple[float, float]]:
    r = subprocess.run([fsproj.ffbin("ffmpeg"), "-hide_banner", "-nostats", "-i", str(wav),
                        "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    regions, start = [], None
    for line in r.stderr.splitlines():
        if "silence_start:" in line:
            try:
                start = float(line.split("silence_start:")[1].split()[0])
            except (IndexError, ValueError):
                start = None
        elif "silence_end:" in line and start is not None:
            try:
                end = float(line.split("silence_end:")[1].split("|")[0].strip())
                regions.append((max(0.0, start), end))
            except (IndexError, ValueError):
                pass
            start = None
    if start is not None:  # 静音一直持续到结尾
        with wave.open(str(wav), "rb") as h:
            regions.append((start, h.getnframes() / h.getframerate()))
    return regions


def analyze_silence(wd: Path, src: Path, silence_db="auto") -> dict:
    """返回 {threshold_db, info, regions:[[a,b],...]}，结果缓存在 silence.json。"""
    wav = ensure_wav(wd, src)
    cache = wd / "silence.json"
    sig = f"{(wd / 'audio.wav.src').read_text('utf-8')}|{silence_db}"
    if cache.exists():
        old = json.loads(cache.read_text("utf-8"))
        if old.get("sig") == sig:
            return old
    wf = Waveform(wav)
    thr, info = resolve_threshold(wf, silence_db)
    regions = detect_silence(wav, thr)
    out = {"sig": sig, "threshold_db": thr, "info": info, "regions": [[round(a, 3), round(b, 3)] for a, b in regions]}
    cache.write_text(json.dumps(out, ensure_ascii=False), "utf-8")
    return out


if __name__ == "__main__":
    fsproj.utf8_stdout()
    pr = fsproj.load(sys.argv[1] if len(sys.argv) > 1 else None)
    wd = fsproj.work_dir(pr)
    res = analyze_silence(wd, fsproj.pick_audio(pr))
    total = sum(b - a for a, b in res["regions"])
    print(json.dumps({"threshold_db": res["threshold_db"], "info": res["info"],
                      "regions": len(res["regions"]), "silence_seconds": round(total, 1)}, ensure_ascii=False))
