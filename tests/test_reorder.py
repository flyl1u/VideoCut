"""reorder.py：调换顺序的分块、时间换算、事件文件改写、媒体重拼。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["VIDEOCUT_HOME"] = tempfile.mkdtemp(prefix="fc-test-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "videocut" / "scripts"))
import reorder  # noqa: E402


class MapperTest(unittest.TestCase):
    def setUp(self):
        # 把 [6, 9] 挪到 2 前面：新顺序 [0,2] [6,9] [2,6] [9,12]
        self.blocks = reorder.plan_blocks([{"start": 6, "end": 9, "before": 2}], 12.0)
        self.f = reorder.Mapper(self.blocks, 12.0)

    def test_blocks(self):
        self.assertEqual([tuple(map(float, b)) for b in self.blocks], [(0, 2), (6, 9), (2, 6), (9, 12)])

    def test_points(self):
        self.assertAlmostEqual(self.f(1.0), 1.0)
        self.assertAlmostEqual(self.f(7.0), 3.0)
        self.assertAlmostEqual(self.f(3.0), 6.0)
        self.assertAlmostEqual(self.f(10.0), 10.0)

    def test_range_split(self):
        # [5, 7] 跨了两块：[5,6] 落在 [2,6] 块尾（新 8–9），[6,7] 落在挪走的块头（新 2–3）
        self.assertEqual([tuple(round(x, 6) for x in r) for r in self.f.range(5, 7)], [(2, 3), (8, 9)])

    def test_playtracks(self):
        tracks = [{"speed": 1.0, "type": 0, "isDisplayCursor": True, "begin": 0.5 / 12, "end": 11 / 12, "duration": 12.0}]
        out = reorder.map_playtracks(tracks, self.f, 12.0)
        self.assertEqual(len(out), 1)  # 整段保留的话挪完还是首尾相接，合并成一段
        self.assertAlmostEqual(out[0]["begin"] * 12, 0.5)
        self.assertAlmostEqual(out[0]["end"] * 12, 11.0)

    def test_snap(self):
        b = reorder.plan_blocks([{"start": 6.01, "end": 9.02, "before": 2.005}], 12.0)
        for a, z in b:
            for t in (a, z):
                self.assertAlmostEqual(t * 30, round(t * 30), places=4)


class EventsTest(unittest.TestCase):
    def test_shift(self):
        d = Path(tempfile.mkdtemp())
        src = d / "m.json"
        lines = ['{"unixTimeMs": %.3f,"cursorId": "arrow","x": 1,"y": 2,"type": "mouseMoved","processTimeMs": %.1f},'
                 % (1e12 + t * 1000, 1000 + t * 1000) for t in (1, 3, 7, 10)]
        src.write_bytes(("[\r\n\r\n" + "\r\n".join(lines) + "\r\n]").encode("utf-8"))
        f = reorder.Mapper(reorder.plan_blocks([{"start": 6, "end": 9, "before": 2}], 12.0), 12.0)
        reorder.shift_events(src, d / "o.json", f, 1000.0)
        txt = (d / "o.json").read_bytes().decode("utf-8")
        self.assertTrue(txt.startswith("[\r\n\r\n") and txt.endswith("\r\n]"))
        import re
        ps = [float(x) for x in re.findall(r'"processTimeMs": ([\d.]+)', txt)]
        self.assertEqual(ps, sorted(ps))
        self.assertEqual(ps, [2000.0, 4000.0, 7000.0, 11000.0])  # t=7→3, t=3→6
        self.assertIn('"unixTimeMs": 1000000003000.000', txt)
        shutil.rmtree(d)


class KeystrokeTest(unittest.TestCase):
    def test_multiline_objects(self):
        d = Path(tempfile.mkdtemp())
        src = d / "k.json"
        objs = ['{"activeModifiers":[\r\n  "alt"\r\n],"character":"%s", "isARepeat":false,"processTimeMs": %.1f,'
                '"type":"keyDown", "unixTimeMs": %.6f }' % (ch, 1000 + t * 1000, 1e12 + t * 1000)
                for ch, t in (("a", 1), ("}", 7), ("b", 3))]
        src.write_bytes(("[\r\n" + ",".join(objs) + "\r\n]").encode("utf-8"))
        f = reorder.Mapper(reorder.plan_blocks([{"start": 6, "end": 9, "before": 2}], 12.0), 12.0)
        self.assertEqual(reorder.shift_events(src, d / "o.json", f, 1000.0), 3)
        txt = (d / "o.json").read_bytes().decode("utf-8")
        data = json.loads(txt)
        self.assertEqual([(e["character"], e["processTimeMs"]) for e in data], [("a", 2000.0), ("}", 4000.0), ("b", 7000.0)])
        self.assertIn('"processTimeMs": 4000.0,', txt)
        self.assertIn('"unixTimeMs": 1000000003000.000000 }', txt)
        self.assertTrue(txt.startswith("[\r\n{") and txt.endswith("}\r\n]"))
        shutil.rmtree(d)


@unittest.skipUnless(shutil.which("ffmpeg"), "需要 ffmpeg")
class MediaTest(unittest.TestCase):
    def test_reorder_av(self):
        d = Path(tempfile.mkdtemp())
        src = d / "src.mp4"
        # 每一秒一种亮度、一种音高（200 + 100×秒 Hz），重拼后按位置核对
        subprocess.run(["ffmpeg", "-y", "-v", "error",
                        "-f", "lavfi", "-i", "nullsrc=s=64x48:r=30:d=12,geq=lum='16+floor(T)*18':cb=128:cr=128",
                        "-f", "lavfi", "-i", "aevalsrc='0.5*sin(2*PI*(200+100*floor(t))*t)':s=44100:d=12",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", str(src)], check=True)
        blocks = reorder.plan_blocks([{"start": 6, "end": 9, "before": 2}], 12.0)
        out = d / "out.mp4"
        reorder.reorder_media(src, out, blocks, 12.0, label="t")
        self.assertAlmostEqual(reorder.media_duration(out), reorder.media_duration(src), delta=0.1)

        def luma(p, t):
            raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(p), "-frames:v", "1",
                                  "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
            return sum(raw) / len(raw)

        def freq(p, t):
            raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-t", "0.4", "-i", str(p), "-vn",
                                  "-ac", "1", "-ar", "44100", "-f", "s16le", "-"], capture_output=True, check=True).stdout
            import array
            a = array.array("h", raw)
            zc = sum(1 for x, y in zip(a, a[1:]) if (x < 0) != (y < 0))
            return zc / 2 / (len(a) / 44100)

        # 新顺序 [0,2] [6,9] [2,6] [9,12]：新 t 对应的源秒数
        for new_t, src_sec in ((1.5, 1), (2.5, 6), (4.5, 8), (5.5, 2), (8.5, 5), (10.5, 10)):
            self.assertAlmostEqual(luma(out, new_t), luma(src, src_sec + 0.5), delta=3, msg=f"画面 新{new_t}s")
            self.assertAlmostEqual(freq(out, new_t), 200 + 100 * src_sec, delta=25, msg=f"声音 新{new_t}s")
        shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
