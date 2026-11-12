"""章节：增删改、成片时间换算（剪辑、调换顺序）、导出时间戳、sentences.txt 标记、调序工程换算。

运行：uv run --no-project python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="videocut-test-")  # 不读本机的全局词表/术语表

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".claude" / "skills" / "videocut" / "scripts"))
import analyze  # noqa: E402
import chapters as chap  # noqa: E402
import reorder  # noqa: E402

D = 100.0


def transcript():
    """10 句，第 k 句在源时间 [10k+1, 10k+9]，每句两个词。"""
    ss = []
    for k in range(10):
        a = 10.0 * k + 1
        ss.append({"id": k, "start": a, "end": a + 8, "text": f"第{k}句。",
                   "words": [{"w": f"甲{k}", "p": "", "start": a, "end": a + 4},
                             {"w": f"乙{k}", "p": "。", "start": a + 4, "end": a + 8}]})
    return {"sentences": ss}


def edit(cuts=(), moves=(), chapters=()):
    return {"duration": D, "params": {}, "base_keeps": [[0.0, D]],
            "cuts": [{"id": f"c{i + 1}", "start": a, "end": b, "reason": "manual", "origin": "user", "enabled": True,
                      "note": ""} for i, (a, b) in enumerate(cuts)],
            "moves": list(moves), "chapters": list(chapters), "sentence_text": {}}


def times(e, tr=None):
    chs, warns, total = chap.resolve(e, tr or transcript(), D)
    return [(c["id"], round(c["start"], 3), round(c["end"], 3)) for c in chs], warns, total


class EditOpsTest(unittest.TestCase):
    def test_add_rename_move_remove(self):
        e, tr = edit(), transcript()
        c1 = chap.set_chapter(e, 0, "开场", transcript=tr)
        c2 = chap.set_chapter(e, 5, "  第二  部分 ", transcript=tr)
        self.assertEqual((c1["id"], c2["id"], c2["title"]), ("ch1", "ch2", "第二 部分"))
        # 同一句再加一次 = 改标题，不会多出一章
        chap.set_chapter(e, 5, "正文", transcript=tr)
        self.assertEqual([(c["sentence"], c["title"]) for c in e["chapters"]], [(0, "开场"), (5, "正文")])
        chap.set_chapter(e, 3, cid="ch2", transcript=tr)
        self.assertEqual(e["chapters"][1]["sentence"], 3)
        with self.assertRaises(ValueError):
            chap.set_chapter(e, 0, cid="ch2", transcript=tr)  # 第 0 句已经是 ch1 的起点
        with self.assertRaises(ValueError):
            chap.set_chapter(e, 99, "不存在", transcript=tr)
        with self.assertRaises(ValueError):
            chap.set_chapter(e, 4, "   ", transcript=tr)
        self.assertEqual(chap.next_chapter_id(e), "ch3")
        self.assertEqual(chap.remove_chapters(e, ["ch1"]), 1)
        self.assertEqual([c["id"] for c in e["chapters"]], ["ch2"])
        self.assertEqual(chap.remove_chapters(e, [""]), 1)  # 不给 id = 全部删除
        self.assertEqual(e["chapters"], [])

    def test_replace(self):
        e = edit(chapters=[{"id": "ch9", "sentence": 2, "title": "旧"}])
        chap.replace_chapters(e, [{"sentence": 6, "title": "后"}, {"sentence": 0, "title": "前"}], transcript())
        self.assertEqual([(c["id"], c["sentence"]) for c in e["chapters"]], [("ch2", 0), ("ch1", 6)])


class ResolveTest(unittest.TestCase):
    def test_no_cuts(self):
        got, warns, total = times(edit(chapters=[{"id": "a", "sentence": 0, "title": "开场"},
                                                 {"id": "b", "sentence": 4, "title": "正文"}]))
        # 第一章总是从 0 开始；第二章 = 第 4 句开头 41s
        self.assertEqual(got, [("a", 0.0, 41.0), ("b", 41.0, 100.0)])
        self.assertEqual((warns, total), ([], 100.0))

    def test_cuts_shift_times(self):
        # 剪掉开头 [0,1]、第 1 句整句 [11,19]、第 3 句后半 [35,39]
        e = edit(cuts=[(0, 1), (11, 19), (35, 39)],
                 chapters=[{"id": "a", "sentence": 0, "title": "开场"}, {"id": "b", "sentence": 4, "title": "正文"}])
        got, warns, total = times(e)
        self.assertEqual(total, 100 - 1 - 8 - 4)
        self.assertEqual(got, [("a", 0.0, 41 - 1 - 8 - 4), ("b", 28.0, 87.0)])

    def test_cut_anchor_rolls_forward(self):
        # 起始句第 2 句整句剪掉（分章时选了重录前那遍）→ 顺延到第 3 句，不算问题
        e = edit(cuts=[(20, 30)], chapters=[{"id": "a", "sentence": 0, "title": "开场"},
                                           {"id": "b", "sentence": 2, "title": "正文"}])
        chs, warns, _ = chap.resolve(e, transcript(), D)
        self.assertEqual((chs[1]["start_sentence"], chs[1]["start"], chs[1]["source_start"]), (3, 21.0, 31.0))
        self.assertEqual(warns, [])

    def test_partly_cut_anchor_uses_first_kept_part(self):
        # 第 4 句前半 [41,45] 剪掉：章节从后半开始（成片 41s）
        e = edit(cuts=[(41, 45)], chapters=[{"id": "a", "sentence": 0, "title": "x"},
                                           {"id": "b", "sentence": 4, "title": "y"}])
        self.assertEqual(times(e)[0][1], ("b", 41.0, 96.0))

    def test_ignored_and_short(self):
        e = edit(cuts=[(80, 100)], chapters=[{"id": "a", "sentence": 0, "title": "开场"},
                                            {"id": "b", "sentence": 7, "title": "短"},
                                            {"id": "c", "sentence": 8, "title": "被剪"},
                                            {"id": "d", "sentence": 42, "title": "旧句"}])
        got, warns, total = times(e)
        self.assertEqual(got, [("a", 0.0, 71.0), ("b", 71.0, 80.0)])
        self.assertTrue(any("c「被剪」" in w and w.endswith("已忽略") for w in warns))
        self.assertTrue(any("d「旧句」" in w and "不存在" in w for w in warns))
        self.assertTrue(any("b「短」只有 9.0 秒" in w for w in warns))

    def test_same_start_deduplicated(self):
        e = edit(cuts=[(20, 30)], chapters=[{"id": "a", "sentence": 2, "title": "一"},
                                           {"id": "b", "sentence": 3, "title": "二"}])
        got, warns, _ = times(e)
        self.assertEqual([g[0] for g in got], ["a"])
        self.assertTrue(any("起点相同" in w for w in warns))

    def test_moves_use_playback_order(self):
        # 第 6 句 [60,70] 挪到第 2 句前面（源 20）：播放顺序 0-20, 60-70, 20-60, 70-100
        mv = {"id": "m1", "start": 60.0, "end": 70.0, "before": 20.0, "enabled": True}
        e = edit(moves=[mv], chapters=[{"id": "a", "sentence": 0, "title": "开场"},
                                      {"id": "b", "sentence": 6, "title": "挪上来的"},
                                      {"id": "c", "sentence": 2, "title": "原第二段"}])
        got, _, total = times(e)
        # 第 6 句在源 61s，挪上来后从成片 20+1=21s 开始；第 2 句在源 21s，成片 30+1=31s
        self.assertEqual(got, [("a", 0.0, 21.0), ("b", 21.0, 31.0), ("c", 31.0, 100.0)])
        self.assertEqual(total, 100.0)
        mv["enabled"] = False  # 停用的 move 不算
        self.assertEqual([g[0] for g in times(e)[0]], ["a", "c", "b"])

    def test_sentence_out_starts_with_moves_and_cuts(self):
        seq = [(0.0, 10.0), (50.0, 60.0), (10.0, 20.0)]
        outs = chap.sentence_out_starts(transcript(), seq)
        self.assertEqual(outs, {0: 1.0, 5: 11.0, 1: 21.0})


class ExportTest(unittest.TestCase):
    def test_fmt(self):
        self.assertEqual(chap.fmt_ts(0), "00:00")
        self.assertEqual(chap.fmt_ts(59.99), "00:59")  # 向下取整，不会跳过章节开头
        self.assertEqual(chap.fmt_ts(754.2), "12:34")
        self.assertEqual(chap.fmt_ts(3723.9, hours=True), "1:02:03")
        self.assertEqual(chap.fmt_ts(65, hours=True), "0:01:05")

    def test_text(self):
        chs = [{"start": 0.0, "title": "开场"}, {"start": 83.7, "title": "正文"}]
        self.assertEqual(chap.export_text(chs, 600), "00:00 开场\n01:23 正文\n")
        self.assertEqual(chap.export_text(chs, 4000), "0:00:00 开场\n0:01:23 正文\n")

    def test_write_export(self):
        wd = Path(tempfile.mkdtemp())
        e = edit(chapters=[{"id": "a", "sentence": 0, "title": "开场"}, {"id": "b", "sentence": 5, "title": "正文"}])
        res = chap.write_export(wd, e, transcript(), D)
        self.assertEqual((wd / "chapters.txt").read_text("utf-8"), "00:00 开场\n00:51 正文\n")
        self.assertEqual(res["text"], "00:00 开场\n00:51 正文\n")
        self.assertEqual(chap.notes(res["chapters"])[0][:4], "只有 2")
        e["chapters"] = []
        self.assertIsNone(chap.write_export(wd, e, transcript(), D))
        self.assertFalse((wd / "chapters.txt").exists())  # 章节删光了，旧导出也删掉


class IntegrationTest(unittest.TestCase):
    def test_sentences_txt_marks_and_summary(self):
        wd = Path(tempfile.mkdtemp())
        e = edit(chapters=[{"id": "ch1", "sentence": 0, "title": "开场"}, {"id": "ch2", "sentence": 3, "title": "正文"}])
        analyze.write_sentences_txt(wd, transcript(), e)
        lines = (wd / "sentences.txt").read_text("utf-8").splitlines()
        i = lines.index("§ ch2「正文」")
        self.assertTrue(lines[i + 1].startswith("[3] "))
        self.assertEqual(analyze.summary(e, D)["chapters"], 2)
        # 复核清单里带上章节问题
        e["chapters"].append({"id": "ch3", "sentence": 9, "title": "太短"})
        e["cuts"].append({"id": "c9", "start": 95.0, "end": 100.0, "reason": "manual", "origin": "user",
                          "enabled": True, "note": ""})
        self.assertTrue(any("ch3「太短」" in w for w in analyze.review_warnings(e, transcript(), D)))

    def test_reorder_remaps_chapter_sentences(self):
        e = edit(chapters=[{"id": "ch1", "sentence": 6, "title": "挪上来的"}])
        e["base_keeps"] = [[0.0, D]]
        f = reorder.Mapper(reorder.plan_blocks([], D), D)
        out = reorder.remap_edit(e, f, {6: 2, 2: 3}, Path("新工程.focusee"))
        self.assertEqual(out["chapters"], [{"id": "ch1", "sentence": 2, "title": "挪上来的"}])
        self.assertEqual(e["chapters"][0]["sentence"], 6)  # 原 edit 不变


if __name__ == "__main__":
    unittest.main()
