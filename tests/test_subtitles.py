"""字幕断句与校对对齐测试（纯算法，不依赖工程文件）。

运行：uv run --no-project python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["VIDEOCUT_HOME"] = tempfile.mkdtemp(prefix="videocut-test-")  # 不读本机的全局词表/术语表

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "videocut" / "scripts"))
import subtitles  # noqa: E402


def mk(sid, items, t0=0.0, step=0.3):
    """items: [(词, 标点)]，每个词按 step 秒依次排开。"""
    words, t = [], t0
    for w, p in items:
        words.append({"w": w, "p": p, "start": round(t, 3), "end": round(t + step * 0.9, 3)})
        t += step
    return {"id": sid, "start": words[0]["start"], "end": words[-1]["end"], "text": "", "words": words}


P = {"max_line_chars": 16, "target_line_chars": 11, "max_line_seconds": 4.2}


def build(sentences, keeps=None, reps=(), fixes=None):
    tr = {"sentences": sentences, "duration": 100.0}
    return subtitles.build(tr, keeps or [(0.0, 100.0)], P, list(reps), fixes or {})


class SplitTest(unittest.TestCase):
    def test_lines_within_limit_and_no_weak_start(self):
        s = mk(0, [("我", ""), ("用", ""), ("AI", ""), ("开发", ""), ("了", ""), ("一个", ""), ("非常", ""),
                   ("简单", ""), ("的", ""), ("前端", ""), ("网页", "，"), ("就是", ""), ("给", ""), ("大家", ""),
                   ("操作", "。")])
        lines = [x["text"] for x in build([s])]
        for ln in lines:
            self.assertLessEqual(subtitles.vlen(ln), 16, ln)
            self.assertFalse(ln.startswith(("的", "了")), ln)
        # 不在「一个」「非常」后面断开
        self.assertFalse(any(ln.endswith(("一个", "非常")) for ln in lines[:-1]), lines)

    def test_prefers_punctuation(self):
        s = mk(0, [("所谓", ""), ("前端", "，"), ("就是", ""), ("用户", ""), ("可以", ""), ("看到", "、"),
                   ("摸到", "、"), ("操作", ""), ("的", ""), ("所有", ""), ("界面", "，"), ("就是", ""), ("前端", "。")])
        lines = [x["text"] for x in build([s])]
        self.assertTrue(all(len(ln) <= 16 for ln in lines))
        self.assertNotIn("，", "".join(lines))  # 句中标点变空格，不显示

    def test_fillers_removed(self):
        s = mk(0, [("嗯", "，"), ("这个", ""), ("按钮", ""), ("啊", "，"), ("很", ""), ("好看", "。")])
        self.assertEqual([x["text"] for x in build([s])], ["这个按钮 很好看"])

    def test_cut_words_dropped(self):
        s = mk(0, [("你", ""), ("可以", ""), ("看", "……"), ("你", ""), ("可以", ""), ("操作", ""), ("什么", "？")])
        # 剪掉前三个词（0~0.9s）
        items = build([s], keeps=[(0.88, 100.0)])
        self.assertEqual([x["text"] for x in items], ["你可以操作什么"])


class FixTest(unittest.TestCase):
    def test_fix_text_aligned_to_times(self):
        s = mk(3, [("你", ""), ("是", ""), ("基数", ""), ("次", ""), ("还是", ""), ("偶数", ""), ("次", "。")])
        items = build([s], fixes={"3": "你是奇数次还是偶数次。"})
        self.assertEqual(items[0]["text"], "你是奇数次还是偶数次")
        self.assertAlmostEqual(items[0]["start"], s["words"][0]["start"])
        self.assertAlmostEqual(items[-1]["end"], s["words"][-1]["end"], places=2)

    def test_forced_break(self):
        s = mk(1, [("我们", ""), ("只有", ""), ("先", ""), ("定好", ""), ("了", ""), ("框架", "")])
        items = build([s], fixes={"1": "我们只有/先定好了框架"})
        self.assertEqual([x["text"] for x in items], ["我们只有", "先定好了框架"])

    def test_ascii_replacement_whole_word(self):
        s = mk(2, [("就是", ""), ("java", ""), ("spring", "，"), ("还有", ""), ("json", "。")])
        items = build([s], reps=[["javaspring", "JavaScript"], ["js", "JS"]])
        text = " ".join(x["text"] for x in items)
        self.assertIn("JavaScript", text)
        self.assertIn("json", text)  # js 不会误改 json
        self.assertNotIn("Java Script", text)

    def test_ascii_replacement_keeps_dotted_names(self):
        s = mk(9, [("用", ""), ("Next.js", ""), ("和", ""), ("js", ""), ("写", "。")])
        text = build([s], reps=[["js", "JS"]])[0]["text"]
        self.assertIn("Next.js", text)
        self.assertIn("和JS", text)


class RegressionTest(unittest.TestCase):
    def test_dotted_ascii_word_kept(self):
        s = mk(4, [("用", ""), ("next", ""), ("点", ""), ("js", ""), ("写", ""), ("前端", "。")])
        items = build([s], fixes={"4": "用Next.js写前端。"})
        self.assertEqual([x["text"] for x in items], ["用Next.js写前端"])
        s2 = mk(5, [("Node.js", ""), ("很", ""), ("快", "。")])
        self.assertEqual([x["text"] for x in build([s2])], ["Node.js很快"])

    def test_manual_pieces_not_resplit(self):
        a = mk(6, [("所以", "，"), ("我们", ""), ("要", ""), ("把", ""), ("这个", ""), ("大", ""), ("任务", ""),
                   ("拆", ""), ("成", ""), ("小", ""), ("任务", "。")])
        b = mk(7, [("然后", ""), ("一个", ""), ("一个", ""), ("去", ""), ("做", "。")], t0=a["end"] + 0.1)
        items = build([a, b], fixes={"6": "所以我们要把这个大任务/拆成小任务。"})
        texts = [x["text"] for x in items]
        self.assertEqual(texts[:2], ["所以我们要把这个大任务", "拆成小任务"])
        self.assertNotIn("所以", texts[2:])

    def test_no_tiny_fragment_line(self):
        s = mk(8, [("所以", "，"), ("我们", ""), ("一般", ""), ("会", ""), ("先", ""), ("开发", ""), ("前端", "，"),
                   ("而且", ""), ("比较", ""), ("简单", "。")])
        texts = [x["text"] for x in build([s])]
        self.assertTrue(all(subtitles.vlen(t) >= 4 for t in texts), texts)


class TermsTest(unittest.TestCase):
    def test_hotwords_dedup_and_append(self):
        import analyze
        self.assertEqual(analyze.add_hotwords(["Claude Code", "DeepSeek", "claude code", " "]), ["Claude Code", "DeepSeek"])
        self.assertEqual(analyze.add_hotwords(["DeepSeek", "Next.js"]), ["Next.js"])
        self.assertEqual(analyze.load_hotwords(), ["Claude Code", "DeepSeek", "Next.js"])

    def test_old_subtitle_style_migrated(self):
        import analyze
        P = analyze.params_of({"params": {"subtitle_style": {"upPadding": 2.0, "bottomPadding": 2.0}}})
        self.assertEqual(P["subtitle_style"]["bottomPadding"], "screen")


class GeometryTest(unittest.TestCase):
    def proj(self, ratio, padding=0.01, w=2560, h=1600):
        import fsproj
        meta = {"recorders": [{"type": "legacyDisplay", "sessions": [{"bounds": {"width": w, "height": h}}]}]}
        conf = {"background": {"ratio": ratio, "padding": padding, "crop": {"rect": {"width": 1, "height": 1}}}}
        return fsproj.Project(Path("."), conf, meta, chr(10), False)

    def test_screen_bottom_margin(self):
        import fsproj
        # 4:3 画布 + 16:10 屏幕：用户截图实测窗口下边缘在约 9% 处
        self.assertAlmostEqual(fsproj.screen_bottom_margin(self.proj("ratio_4_3")), 9.0, delta=0.3)
        self.assertAlmostEqual(fsproj.screen_bottom_margin(self.proj("auto", 0.04)), 4.0, delta=0.1)
        self.assertGreater(fsproj.screen_bottom_margin(self.proj("ratio_9_16")), 30)


class ScrollTest(unittest.TestCase):
    def test_scroll_does_not_block_semantic_cut(self):
        import analyze

        class Ev:
            wave = None
            events = []

            def events_in(self, a, b):
                return [e for e in self.events if a <= e[0] <= b]

            def quiet(self, *a, **k):
                return None

        s = mk(0, [("那", ""), ("那", ""), ("我们", ""), ("来", ""), ("看", "。")])
        words = analyze.all_words({"sentences": [s]})
        edit = {"cuts": []}
        ev = Ev()
        ev.events = [(0.1, "scroll"), (0.2, "scroll")]
        c = analyze.semantic_cut(edit, words, ev, analyze.DEFAULT_PARAMS, span=(0, 0))
        self.assertTrue(c["enabled"])
        self.assertIn("滚动", c["note"])
        ev.events = [(0.1, "click")]
        c2 = analyze.semantic_cut(edit, words, ev, analyze.DEFAULT_PARAMS, span=(0, 0))
        self.assertFalse(c2["enabled"])


if __name__ == "__main__":
    unittest.main()
