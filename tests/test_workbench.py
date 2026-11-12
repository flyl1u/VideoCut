"""工作台与数据目录自测：旧数据迁移、工程列表、URL 防穿越、HTTP 路由。

运行：uv run --no-project python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("VIDEOCUT_HOME", tempfile.mkdtemp(prefix="videocut-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "videocut" / "scripts"))
import fsproj  # noqa: E402
import workbench  # noqa: E402


class MigrateTest(unittest.TestCase):
    def test_copies_legacy_and_package_cache(self):
        local = Path(tempfile.mkdtemp(prefix="vc-local-"))
        (local / "videocut" / "projA-1").mkdir(parents=True)
        (local / "videocut" / "hotwords.txt").write_text("旧的\n", "utf-8")
        (local / "videocut" / "bg").mkdir()
        pkg = local / "Packages" / "Claude_x" / "LocalCache" / "Local" / "videocut"
        (pkg / "projB-2").mkdir(parents=True)
        (pkg / "hotwords.txt").write_text("商店应用里的\n", "utf-8")
        old_env = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = str(local)
        try:
            dest = Path(tempfile.mkdtemp(prefix="vc-home-")) / ".videocut"
            fsproj.migrate_legacy(dest)
        finally:
            if old_env is None:
                os.environ.pop("LOCALAPPDATA", None)
            else:
                os.environ["LOCALAPPDATA"] = old_env
        self.assertTrue((dest / "projA-1").is_dir())
        self.assertTrue((dest / "projB-2").is_dir())
        self.assertEqual((dest / "hotwords.txt").read_text("utf-8"), "商店应用里的\n")  # 商店应用里的更新，覆盖旧的
        self.assertFalse((dest / "bg").exists())  # 后台任务状态不搬
        self.assertTrue((local / "videocut" / "hotwords.txt").exists())  # 旧目录原样保留


def make_wd(name: str, project: str, duration: float = 10.0) -> Path:
    wd = fsproj.data_root() / name
    wd.mkdir(parents=True, exist_ok=True)
    edit = {"cuts": [], "base_keeps": [[0, duration]], "duration": duration}
    (wd / "edit.json").write_text(json.dumps(edit), "utf-8")
    (wd / "project_path.txt").write_text(project, "utf-8")
    return wd


class WorkbenchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proj = Path(tempfile.mkdtemp(prefix="vc-proj-")) / "录屏 A.focusee"
        cls.proj.mkdir()
        (cls.proj / fsproj.PROJ_FILE).write_text("{}", "utf-8")
        make_wd("录屏 A-aaaa1111", str(cls.proj))
        make_wd("没了-bbbb2222", str(cls.proj.parent / "没了.focusee"))
        make_wd("测试副本-cccc3333", r"_testdata\测试副本.focusee")  # 相对路径：单元测试的临时副本
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), workbench.Handler)
        cls.port = cls.srv.server_port
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def get(self, path):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        try:
            r = opener.open(f"http://127.0.0.1:{self.port}{path}", timeout=5)
            return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_list_projects(self):
        items = {p["key"]: p for p in workbench.list_projects()["projects"]}
        self.assertIn("录屏 A-aaaa1111", items)
        self.assertTrue(items["录屏 A-aaaa1111"]["exists"])
        self.assertFalse(items["没了-bbbb2222"]["exists"])
        self.assertNotIn("测试副本-cccc3333", items)
        self.assertEqual(items["录屏 A-aaaa1111"]["summary"]["after"], 10.0)

    def test_find_wd_rejects_traversal(self):
        for bad in ("..", "../x", "..\\x", ".videocut", "", "a/b", "不存在-0000"):
            self.assertIsNone(workbench.find_wd(bad), bad)
        self.assertIsNotNone(workbench.find_wd("录屏 A-aaaa1111"))

    def test_routes(self):
        code, _, body = self.get("/api/hello")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["app"], workbench.APP)
        code, h, _ = self.get("/p/%E5%BD%95%E5%B1%8F%20A-aaaa1111")
        self.assertEqual(code, 302)
        self.assertTrue(h["Location"].endswith("/"))
        code, _, body = self.get("/p/%E5%BD%95%E5%B1%8F%20A-aaaa1111/")
        self.assertEqual(code, 200)
        self.assertIn(b"<html", body[:200])
        code, h, _ = self.get("/p/..%5Cbg/api/data")
        self.assertEqual(code, 302)
        self.assertIn("/?missing=", h["Location"])
        code, _, _ = self.get("/p/%E6%B2%A1%E4%BA%86-bbbb2222/api/data")
        self.assertEqual(code, 410)

    def post(self, path, obj):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(obj).encode(),
                                     method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_rename_display_only(self):
        code, j = self.post("/api/rename", {"project": str(self.proj), "name": "  第一期   开场  "})
        self.assertEqual((code, j["name"]), (200, "第一期 开场"))
        items = {p["key"]: p for p in workbench.list_projects()["projects"]}
        self.assertEqual(items["录屏 A-aaaa1111"]["name"], "第一期 开场")
        self.assertEqual(items["录屏 A-aaaa1111"]["original"], "录屏 A")
        self.assertTrue(self.proj.exists())  # 工程文件夹没动
        code, j = self.post("/api/rename", {"project": str(self.proj), "name": ""})  # 清空恢复原名
        self.assertEqual(j["name"], "录屏 A")
        self.assertNotIn(workbench.name_key(self.proj), workbench.load_names())
        code, _ = self.post("/api/rename", {"project": r"C:\Windows", "name": "x"})
        self.assertEqual(code, 404)  # 只能改工作台里列出的工程

    def test_post_rejects_foreign_origin(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/quit", data=b"{}", method="POST",
                                     headers={"Origin": "http://evil.example", "Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
