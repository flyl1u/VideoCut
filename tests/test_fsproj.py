"""fsproj 自测：区间运算、比例换算往返、工程文件原样读写。

运行：uv run --no-project python -m unittest discover -s tests -v
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".claude" / "skills" / "videocut" / "scripts"))
import fsproj  # noqa: E402

PROJECTS = fsproj.DEFAULT_PROJECTS_DIR


class RangeTest(unittest.TestCase):
    def test_merge(self):
        self.assertEqual(fsproj.merge([(3, 4), (1, 2), (1.5, 2.5)]), [(1, 2.5), (3, 4)])

    def test_complement(self):
        self.assertEqual(fsproj.complement([(1, 2), (5, 12)], 10), [(0, 1), (2, 5)])
        self.assertEqual(fsproj.complement([], 10), [(0, 10)])

    def test_intersect(self):
        self.assertEqual(fsproj.intersect([(0, 5), (6, 10)], [(4, 7)]), [(4, 5), (6, 7)])

    def test_playtracks_roundtrip(self):
        total = 210.066667
        keeps = [(0.0, 15.1), (20.5, 146.45), (159.06, total)]
        tpl = {"speed": 1.0, "type": 0, "isDisplayCursor": True, "begin": 0, "end": 1, "duration": total}
        tracks = fsproj.keeps_to_playtracks(keeps, total, tpl)
        self.assertEqual(len(tracks), 3)
        self.assertEqual(tracks[-1]["end"], 1.0)
        self.assertEqual(list(tracks[0].keys()), ["speed", "type", "isDisplayCursor", "begin", "end", "duration"])
        back = fsproj.playtracks_to_keeps(tracks, total)
        for (a, b), (c, d) in zip(keeps, back):
            self.assertAlmostEqual(a, c, places=6)
            self.assertAlmostEqual(b, d, places=6)

    def test_drop_tiny_keeps(self):
        tracks = fsproj.keeps_to_playtracks([(0, 5), (6, 6.1), (7, 10)], 10)
        self.assertEqual(len(tracks), 2)


@unittest.skipUnless(PROJECTS.exists(), "本机没有 FocuSee 工程目录")
class RealProjectTest(unittest.TestCase):
    def test_byte_identical_roundtrip(self):
        n = 0
        for d in sorted(PROJECTS.glob("*.focusee")):
            f = d / fsproj.PROJ_FILE
            if not f.exists():
                continue
            pr = fsproj.load(d)
            out = fsproj.dumps(pr.conf, pr.newline, pr.trailing_newline).encode("utf-8")
            self.assertEqual(out, f.read_bytes(), f"原样写回不一致：{d.name}")
            n += 1
        self.assertGreater(n, 0)

    def test_existing_playtracks(self):
        # 挑一个已经手动剪过（多段 playTracks）的工程
        d = next((x for x in sorted(PROJECTS.glob("*.focusee"))
                  if (x / fsproj.PROJ_FILE).exists() and len(fsproj.load(x).conf.get("playTracks") or []) > 1), None)
        if d is None:
            self.skipTest("没有剪过的样例工程")
        pr = fsproj.load(d)
        keeps = fsproj.playtracks_to_keeps(pr.conf["playTracks"], pr.duration)
        rebuilt = fsproj.keeps_to_playtracks(keeps, pr.duration, pr.conf["playTracks"][0])
        for a, b in zip(rebuilt, pr.conf["playTracks"]):
            self.assertAlmostEqual(a["begin"], b["begin"], places=10)
            self.assertAlmostEqual(a["end"], b["end"], places=10)


TEST_PROJECT = Path(__file__).resolve().parents[1] / "_testdata" / "测试副本 2026-05-01.focusee"


@unittest.skipUnless((TEST_PROJECT / fsproj.PROJ_FILE).exists(), "没有测试副本")
class ApplyGuardTest(unittest.TestCase):
    def test_refuse_when_focusee_running(self):
        import apply
        pr = fsproj.load(TEST_PROJECT)
        if not (fsproj.work_dir(pr) / "edit.json").exists():
            self.skipTest("测试副本还没有 edit.json")
        before = pr.conf_path.read_bytes()
        orig = fsproj.focusee_running
        fsproj.focusee_running = lambda: ["FocuSee.exe"]
        try:
            with self.assertRaises(apply.FocuSeeRunning):
                apply.apply_edit(TEST_PROJECT)
            self.assertEqual(before, pr.conf_path.read_bytes())
            res = apply.apply_edit(TEST_PROJECT, dry_run=True)  # 预演不受影响
            self.assertTrue(res["dry_run"])
        finally:
            fsproj.focusee_running = orig


if __name__ == "__main__":
    unittest.main()
