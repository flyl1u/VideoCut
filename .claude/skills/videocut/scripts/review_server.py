# /// script
# requires-python = ">=3.10"
# ///
"""本地审阅服务：提供审阅页、预览视频（支持 Range 拖动）、识别结果和剪辑方案，保存时写回 FocuSee 工程。

用法：uv run review_server.py --project "<xxx.focusee>" [--port 8765]
只监听 127.0.0.1。启动后打印 READY http://127.0.0.1:<port>/
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import analyze  # noqa: E402
import apply as applier  # noqa: E402
import fsproj  # noqa: E402
import subtitles  # noqa: E402

ASSETS = HERE.parent / "assets"
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    project_dir: Path
    wd: Path

    def log_message(self, fmt, *args):  # 安静一点
        pass

    # ---------------------------------------------------------- 工具
    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _file(self, path: Path, ctype: str):
        if not path.exists():
            return self._json(404, {"error": f"{path.name} 不存在"})
        size = path.stat().st_size
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        code = 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                elif m.group(2):  # bytes=-N
                    start = max(0, size - int(m.group(2)))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(path, "rb") as f:
                f.seek(start)
                remain = length
                while remain > 0:
                    chunk = f.read(min(1 << 20, remain))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remain -= len(chunk)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass

    # ---------------------------------------------------------- 路由
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._file(ASSETS / "review.html", "text/html; charset=utf-8")
        if path == "/proxy.mp4":
            return self._file(self.wd / "proxy.mp4", "video/mp4")
        if path == "/api/data":
            pr = fsproj.load(self.project_dir)
            edit = analyze.load_edit(self.wd)
            transcript = json.loads((self.wd / "transcript.json").read_text("utf-8"))
            return self._json(200, {
                "project": str(pr.dir),
                "name": pr.name,
                "duration": pr.duration,
                "proxy": (self.wd / "proxy.mp4").exists(),
                "transcript": transcript,
                "edit": edit,
                "summary": analyze.summary(edit, pr.duration) if edit else None,
            })
        if path == "/api/reorder":
            with REORDER_LOCK:
                return self._json(200, {k: v for k, v in REORDER.items() if k != "log"} | {"log": REORDER["log"][-8:]})
        if path == "/api/status":
            return self._json(200, {"proxy": (self.wd / "proxy.mp4").exists(),
                                    "focusee_running": fsproj.focusee_running()})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        path, _, query = self.path.partition("?")
        if path not in ("/api/save", "/api/preview", "/api/doc", "/api/reorder"):
            return self._json(404, {"error": "not found"})
        if self.headers.get("Origin") not in (None, f"http://127.0.0.1:{self.server.server_port}",
                                              f"http://localhost:{self.server.server_port}"):
            return self._json(403, {"error": "forbidden origin"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            edit = json.loads(self.rfile.read(n).decode("utf-8"))
            assert isinstance(edit.get("cuts"), list) and isinstance(edit.get("base_keeps"), list)
        except Exception as e:
            return self._json(400, {"error": f"edit.json 格式不对：{e}"})
        if path == "/api/reorder":  # 调换顺序：后台重拼素材生成新工程
            return self._json(*start_reorder(self.project_dir, self.wd, edit))
        if path == "/api/doc":  # 逐字稿：每句显示文字 + 逐字时间，不写文件
            try:
                transcript = json.loads((self.wd / "transcript.json").read_text("utf-8"))
                doc = subtitles.doc_tokens(transcript, analyze.params_of(edit), edit.get("replacements") or [],
                                           edit.get("sentence_text") or {})
                return self._json(200, {"sentences": doc})
            except Exception as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        if path == "/api/preview":  # 只算字幕，不写任何文件
            try:
                pr = fsproj.load(self.project_dir)
                transcript = json.loads((self.wd / "transcript.json").read_text("utf-8"))
                _, _, items = applier.compute(pr, self.wd, edit, transcript)
                return self._json(200, {"items": items})
            except Exception as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        force = "force=1" in query
        with LOCK:
            try:
                res = applier.apply_edit(self.project_dir, edit=edit, force=force)
            except applier.FocuSeeRunning as e:
                return self._json(409, {"error": str(e), "focusee_running": True})
            except Exception as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        analyze.write_sentences_txt(self.wd, json.loads((self.wd / "transcript.json").read_text("utf-8")), edit)
        return self._json(200, res)


REORDER_LOCK = threading.Lock()
REORDER = {"state": "idle", "progress": "", "log": [], "result": None, "error": None}


def start_reorder(project_dir: Path, wd: Path, edit: dict) -> tuple[int, dict]:
    """后台跑 reorder.py（重新编码要好几分钟），页面轮询 GET /api/reorder 看进度。"""
    if not [m for m in edit.get("moves") or [] if m.get("enabled", True)]:
        return 400, {"error": "没有调换顺序的记录"}
    with REORDER_LOCK:
        if REORDER["state"] == "running":
            return 409, {"error": "已经在生成了"}
        REORDER.update(state="running", progress="准备中", log=[], result=None, error=None)
    ep = wd / "reorder_edit.json"
    ep.write_text(json.dumps(edit, ensure_ascii=False), "utf-8")
    cmd = [sys.executable, str(Path(__file__).resolve().parent / "reorder.py"),
           "--project", str(project_dir), "--edit", str(ep)]

    def run():
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                 encoding="utf-8", errors="replace", env=os.environ | {"PYTHONIOENCODING": "utf-8"})
            for line in p.stdout:
                line = line.rstrip()
                with REORDER_LOCK:
                    REORDER["log"].append(line)
                    if line.startswith("PROGRESS "):
                        REORDER["progress"] = line[9:]
                    elif line.startswith("DONE "):
                        REORDER["result"] = json.loads(line[5:])
                    elif line.startswith("重拼 "):
                        REORDER["progress"] = line
            code = p.wait()
            with REORDER_LOCK:
                if code == 0 and REORDER["result"]:
                    REORDER["state"] = "done"
                else:
                    REORDER["state"] = "error"
                    REORDER["error"] = chr(10).join(REORDER["log"][-6:]) or f"退出码 {code}"
        except Exception as e:
            with REORDER_LOCK:
                REORDER.update(state="error", error=f"{type(e).__name__}: {e}")

    threading.Thread(target=run, daemon=True).start()
    return 200, {"state": "running"}


def free_port(preferred: int) -> int:
    for p in [preferred] + list(range(preferred + 1, preferred + 20)):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    fsproj.utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--project")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true", help="不自动用默认浏览器打开审阅页")
    args = ap.parse_args()
    pr = fsproj.load(args.project)
    wd = fsproj.work_dir(pr)
    if not (wd / "edit.json").exists():
        sys.exit("还没有 edit.json，请先运行 transcribe.py 和 analyze.py auto")
    Handler.project_dir = pr.dir
    Handler.wd = wd
    port = free_port(args.port)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"READY {url}", flush=True)
    if not args.no_open:
        import webbrowser
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
