# /// script
# requires-python = ">=3.10"
# ///
"""VideoCut 工作台：一个常驻的本地服务，管理所有粗剪过的工程，不依赖 agent（关掉 Claude Code / Codex 也照样能打开、编辑、保存）。

  http://127.0.0.1:8765/              工作台首页：粗剪过的工程列表、最近还没粗剪的录屏
  http://127.0.0.1:8765/p/<工作目录名>/  某个工程的审阅页（预览、还原/追加剪辑、改字幕、章节、调序、保存到工程）

用法：
  uv run workbench.py                       确保工作台在运行（没运行就在后台启动），打开首页
  uv run workbench.py --project "<工程>"    同上，直接打开这个工程的审阅页
  uv run workbench.py --no-open             只确保在运行，不打开浏览器
  uv run workbench.py --stop                停掉工作台
都会打印 READY <地址>。服务在独立的后台进程里运行，命令本身马上返回。
只监听 127.0.0.1。代码更新后（比如 git pull），下次启动时会自动换成新版本（正在生成调序工程时不换）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import analyze  # noqa: E402
import apply as applier  # noqa: E402
import chapters as chap  # noqa: E402
import fsproj  # noqa: E402
import subtitles  # noqa: E402

APP = "videocut-workbench"
DEFAULT_PORT = 8765
ASSETS = HERE.parent / "assets"
SAVE_LOCK = threading.Lock()


def code_sig() -> str:
    """脚本文件的指纹：工作台启动后代码有更新，就换成新版本。"""
    h = hashlib.sha1()
    for p in sorted(HERE.glob("*.py")):
        st = p.stat()
        h.update(f"{p.name}:{st.st_size}:{int(st.st_mtime)}".encode())
    return h.hexdigest()[:12]


SIG = code_sig()


def state_file() -> Path:
    return fsproj.data_root() / "workbench.json"


# ---------------------------------------------------------------- 工程

def project_of(wd: Path) -> Path | None:
    """工作目录对应的工程文件夹（记录在 project_path.txt）；工程被移动或删掉了返回 None。"""
    try:
        p = Path((wd / "project_path.txt").read_text("utf-8").strip())
    except OSError:
        return None
    if not p.is_absolute() or not (p / fsproj.PROJ_FILE).exists():
        return None
    return p


def work_dirs() -> list[Path]:
    root = fsproj.data_root()
    return [d for d in root.iterdir() if d.is_dir() and (d / "edit.json").exists() and (d / "project_path.txt").exists()]


def find_wd(key: str) -> Path | None:
    """URL 里的 key 就是工作目录名；只认 data_root 下真实存在的工作目录，防止路径穿越。"""
    if not key or "/" in key or "\\" in key or key.startswith("."):
        return None
    d = fsproj.data_root() / key
    return d if d.is_dir() and (d / "edit.json").exists() else None


def project_url(port: int, wd: Path) -> str:
    return f"http://127.0.0.1:{port}/p/{urllib.parse.quote(wd.name)}/"


# ---------------------------------------------------------------- 显示名（只给工作台看，不改 FocuSee 工程）

NAMES_FILE = "display_names.json"
NAMES_LOCK = threading.Lock()


def name_key(project: str | Path) -> str:
    return str(Path(project)).rstrip("\\/").lower()


def load_names() -> dict[str, str]:
    try:
        data = json.loads((fsproj.data_root() / NAMES_FILE).read_text("utf-8"))
        return {k: v for k, v in data.items() if isinstance(v, str)}
    except (OSError, ValueError, AttributeError):
        return {}


def display_name(project: str | Path, names: dict[str, str] | None = None) -> str:
    names = load_names() if names is None else names
    return names.get(name_key(project)) or Path(project).stem


def set_display_name(project: str, name: str) -> str:
    """给工程起个只在工作台里显示的名字；空字符串表示恢复成工程原名。返回最终显示的名字。"""
    name = " ".join(str(name or "").split())[:80]
    with NAMES_LOCK:
        names = load_names()
        k = name_key(project)
        if not name or name == Path(project).stem:
            names.pop(k, None)
        else:
            names[k] = name
        p = fsproj.data_root() / NAMES_FILE
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(names, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, p)
    return name or Path(project).stem


def list_projects() -> dict:
    names = load_names()
    items = []
    known = set()
    for wd in work_dirs():
        try:
            edit = json.loads((wd / "edit.json").read_text("utf-8"))
        except (OSError, ValueError):
            continue
        proj = project_of(wd)
        raw = (wd / "project_path.txt").read_text("utf-8").strip()
        if not Path(raw).is_absolute():  # 单元测试留下的临时副本，不列出来
            continue
        if proj:
            known.add(str(proj.resolve()).lower())
        try:
            s = analyze.summary(edit, float(edit.get("duration") or 0)) if edit.get("duration") else None
        except Exception:
            s = None
        items.append({
            "key": wd.name,
            "name": display_name(raw, names),
            "original": Path(raw).stem,
            "project": raw,
            "exists": proj is not None,
            "modified": max((wd / "edit.json").stat().st_mtime,
                            (proj / fsproj.PROJ_FILE).stat().st_mtime if proj else 0),
            "summary": s,
            "proxy": (wd / "proxy.mp4").exists(),
            "chapters_file": (wd / chap.EXPORT_NAME).exists(),
            "reordered": "调序" in Path(raw).stem,
        })
    items.sort(key=lambda x: (not x["exists"], -x["modified"]))
    fresh = []
    try:
        cands = [p for p in fsproj.DEFAULT_PROJECTS_DIR.glob("*.focusee") if (p / fsproj.PROJ_FILE).exists()]
        cands.sort(key=lambda p: (p / fsproj.PROJ_FILE).stat().st_mtime, reverse=True)
        for p in cands:
            if str(p.resolve()).lower() in known:
                continue
            fresh.append({"name": display_name(p, names), "original": p.stem, "project": str(p),
                          "modified": (p / fsproj.PROJ_FILE).stat().st_mtime})
            if len(fresh) >= 6:
                break
    except OSError:
        pass
    return {"projects": items, "fresh": fresh, "projects_dir": str(fsproj.DEFAULT_PROJECTS_DIR)}


# ---------------------------------------------------------------- 调序任务（每个工程一份状态）

REORDER_LOCK = threading.Lock()
REORDERS: dict[str, dict] = {}


def reorder_state(key: str) -> dict:
    with REORDER_LOCK:
        return REORDERS.setdefault(key, {"state": "idle", "progress": "", "log": [], "result": None, "error": None})


def any_reorder_running() -> bool:
    with REORDER_LOCK:
        return any(r["state"] == "running" for r in REORDERS.values())


def start_reorder(key: str, project_dir: Path, wd: Path, edit: dict) -> tuple[int, dict]:
    """后台跑 reorder.py（重新编码要好几分钟），页面轮询 GET api/reorder 看进度。"""
    if not [m for m in edit.get("moves") or [] if m.get("enabled", True)]:
        return 400, {"error": "没有调换顺序的记录"}
    st = reorder_state(key)
    with REORDER_LOCK:
        if st["state"] == "running":
            return 409, {"error": "已经在生成了"}
        st.update(state="running", progress="准备中", log=[], result=None, error=None)
    ep = wd / "reorder_edit.json"
    ep.write_text(json.dumps(edit, ensure_ascii=False), "utf-8")
    cmd = [sys.executable, str(HERE / "reorder.py"), "--project", str(project_dir), "--edit", str(ep)]

    def run():
        try:
            kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 text=True, encoding="utf-8", errors="replace",
                                 env=os.environ | {"PYTHONIOENCODING": "utf-8"}, **kw)
            for line in p.stdout:
                line = line.rstrip()
                with REORDER_LOCK:
                    st["log"].append(line)
                    if line.startswith("PROGRESS "):
                        st["progress"] = line[9:]
                    elif line.startswith("DONE "):
                        st["result"] = json.loads(line[5:])
                    elif line.startswith("重拼 "):
                        st["progress"] = line
            code = p.wait()
            with REORDER_LOCK:
                if code == 0 and st["result"]:
                    st["state"] = "done"
                    res = st["result"]
                    nwd = res.get("work_dir")
                    if nwd and Path(nwd).is_dir():  # 新工程的审阅页地址，页面上直接给链接
                        st["result"]["review_url"] = f"/p/{urllib.parse.quote(Path(nwd).name)}/"
                else:
                    st["state"] = "error"
                    st["error"] = chr(10).join(st["log"][-6:]) or f"退出码 {code}"
        except Exception as e:
            with REORDER_LOCK:
                st.update(state="error", error=f"{type(e).__name__}: {e}")

    threading.Thread(target=run, daemon=True).start()
    return 200, {"state": "running"}


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
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

    def _redirect(self, to: str):
        self._send(302, b"", "text/plain", {"Location": to})

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
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
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
            self.close_connection = True

    def _route(self):
        """返回 (key, wd, 子路径)；不是工程页面时 key 为 None。"""
        path = urllib.parse.unquote(self.path.split("?", 1)[0])
        m = re.match(r"^/p/([^/]+)(/.*)?$", path)
        if not m:
            return None, None, path
        return m.group(1), find_wd(m.group(1)), m.group(2)

    def _origin_ok(self) -> bool:
        port = self.server.server_port
        return self.headers.get("Origin") in (None, f"http://127.0.0.1:{port}", f"http://localhost:{port}")

    # ---------------------------------------------------------- 路由
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        key, wd, sub = self._route()
        if key is None:
            if sub in ("/", "/index.html"):
                return self._file(ASSETS / "workbench.html", "text/html; charset=utf-8")
            if sub in ("/favicon.svg", "/favicon.ico"):
                return self._file(ASSETS / "icon" / "videocut.svg", "image/svg+xml")
            if sub == "/api/hello":
                return self._json(200, {"app": APP, "sig": SIG, "pid": os.getpid()})
            if sub == "/api/projects":
                return self._json(200, list_projects())
            return self._json(404, {"error": "not found"})
        if wd is None:
            return self._redirect("/?missing=" + urllib.parse.quote(key))
        if sub is None:  # /p/<key> → /p/<key>/，页面里的相对地址才对
            return self._redirect(f"/p/{urllib.parse.quote(key)}/")
        proj = project_of(wd)
        if sub in ("/", "/index.html"):
            return self._file(ASSETS / "review.html", "text/html; charset=utf-8")
        if sub == "/proxy.mp4":
            return self._file(wd / "proxy.mp4", "video/mp4")
        if proj is None:
            return self._json(410, {"error": "找不到这个工程了（被移动、改名或删除）"})
        if sub == "/api/data":
            pr = fsproj.load(proj)
            edit = analyze.load_edit(wd)
            transcript = json.loads((wd / "transcript.json").read_text("utf-8"))
            return self._json(200, {
                "project": str(pr.dir),
                "name": display_name(pr.dir),
                "original": pr.name,
                "duration": pr.duration,
                "proxy": (wd / "proxy.mp4").exists(),
                "transcript": transcript,
                "edit": edit,
                "summary": analyze.summary(edit, pr.duration) if edit else None,
            })
        if sub == "/api/reorder":
            st = reorder_state(key)
            with REORDER_LOCK:
                return self._json(200, {k: v for k, v in st.items() if k != "log"} | {"log": st["log"][-8:]})
        if sub == "/api/status":
            return self._json(200, {"proxy": (wd / "proxy.mp4").exists(),
                                    "focusee_running": fsproj.focusee_running()})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._origin_ok():
            return self._json(403, {"error": "forbidden origin"})
        key, wd, sub = self._route()
        query = self.path.partition("?")[2]
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if key is None:
            if sub == "/api/quit":
                if any_reorder_running():
                    return self._json(409, {"error": "正在生成调序工程，等它完成再退出"})
                self._json(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if sub == "/api/rename":  # 只改工作台里的显示名，不动 FocuSee 的文件夹和工程
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                    project = str(body.get("project") or "")
                    known = {name_key(x["project"]) for x in (lambda d: d["projects"] + d["fresh"])(list_projects())}
                    if name_key(project) not in known:
                        return self._json(404, {"error": "工作台里没有这个工程"})
                    return self._json(200, {"name": set_display_name(project, body.get("name") or "")})
                except Exception as e:
                    return self._json(400, {"error": f"{type(e).__name__}: {e}"})
            return self._json(404, {"error": "not found"})
        if wd is None:
            return self._json(404, {"error": "没有这个工程"})
        proj = project_of(wd)
        if proj is None:
            return self._json(410, {"error": "找不到这个工程了（被移动、改名或删除）"})
        if sub not in ("/api/save", "/api/preview", "/api/doc", "/api/reorder", "/api/chapters"):
            return self._json(404, {"error": "not found"})
        try:
            edit = json.loads(raw.decode("utf-8"))
            assert isinstance(edit.get("cuts"), list) and isinstance(edit.get("base_keeps"), list)
        except Exception as e:
            return self._json(400, {"error": f"edit.json 格式不对：{e}"})
        if sub == "/api/reorder":  # 调换顺序：后台重拼素材生成新工程
            return self._json(*start_reorder(key, proj, wd, edit))
        try:
            transcript = json.loads((wd / "transcript.json").read_text("utf-8"))
        except (OSError, ValueError) as e:
            return self._json(500, {"error": f"读不到 transcript.json：{e}"})
        if sub == "/api/doc":  # 逐字稿：每句显示文字 + 逐字时间，不写文件
            try:
                doc = subtitles.doc_tokens(transcript, analyze.params_of(edit), edit.get("replacements") or [],
                                           edit.get("sentence_text") or {})
                return self._json(200, {"sentences": doc})
            except Exception as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        if sub == "/api/chapters":  # 章节在成片里的时间 + 导出文本，不写文件
            try:
                pr = fsproj.load(proj)
                chs, warns, total = chap.resolve(edit, transcript, pr.duration)
                return self._json(200, {"chapters": chs, "warnings": warns + chap.notes(chs), "total": total,
                                        "text": chap.export_text(chs, total), "file": str(wd / chap.EXPORT_NAME)})
            except Exception as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        if sub == "/api/preview":  # 只算字幕，不写任何文件
            try:
                pr = fsproj.load(proj)
                _, _, items = applier.compute(pr, wd, edit, transcript)
                return self._json(200, {"items": items})
            except Exception as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        force = "force=1" in query
        with SAVE_LOCK:
            try:
                res = applier.apply_edit(proj, edit=edit, force=force)
            except applier.FocuSeeRunning as e:
                return self._json(409, {"error": str(e), "focusee_running": True})
            except Exception as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        analyze.write_sentences_txt(wd, transcript, edit)
        return self._json(200, res)


# ---------------------------------------------------------------- 启动 / 复用 / 停止

def hello(port: int, timeout: float = 0.8) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/hello", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data if data.get("app") == APP else None
    except Exception:
        return None


def quit_server(port: int) -> bool:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/quit", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            r.read()
    except Exception:
        return False
    for _ in range(30):
        if hello(port, 0.3) is None:
            return True
        time.sleep(0.2)
    return False


def port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def known_port() -> int | None:
    try:
        return int(json.loads(state_file().read_text("utf-8"))["port"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def running_port() -> int | None:
    for p in dict.fromkeys([known_port() or DEFAULT_PORT, DEFAULT_PORT]):
        if hello(p):
            return p
    return None


def spawn_server(port: int) -> None:
    """在独立的后台进程里启动服务（脱离当前终端 / agent，关掉它们也不受影响）。"""
    import bg
    bg.spawn("workbench", [bg.stable_python(), str(Path(__file__).resolve()), "--serve", "--port", str(port)])


def ensure_running() -> int:
    port = running_port()
    if port:
        info = hello(port) or {}
        if info.get("sig") == SIG:
            return port
        # 代码更新过：换成新版本（正在生成调序工程时 quit 会被拒绝，就先接着用旧的）
        if not quit_server(port):
            return port
    port = DEFAULT_PORT
    while not port_free(port):
        port += 1
        if port > DEFAULT_PORT + 30:
            sys.exit("找不到可用端口")
    spawn_server(port)
    for _ in range(100):
        if hello(port, 0.3):
            return port
        time.sleep(0.15)
    import bg
    log = bg.paths("workbench")[1]
    sys.exit(f"工作台没能启动，日志：{log}\n" + "\n".join(bg.tail(log)))


def serve(port: int) -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    state_file().write_text(json.dumps({"port": port, "pid": os.getpid(), "sig": SIG,
                                        "started": time.strftime("%Y-%m-%d %H:%M:%S")}), "utf-8")
    print(f"SERVING http://127.0.0.1:{port}/", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def main():
    fsproj.utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", help="直接打开这个工程的审阅页")
    ap.add_argument("--no-open", action="store_true", help="不自动用默认浏览器打开")
    ap.add_argument("--stop", action="store_true", help="停掉工作台")
    ap.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)  # 内部用：在前台跑服务
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.serve:
        return serve(args.port)
    if args.stop:
        port = running_port()
        if not port:
            print("工作台没有在运行")
        elif quit_server(port):
            print("STOPPED")
        else:
            sys.exit("停不下来：可能正在生成调序工程，等它完成再试")
        return

    target_wd = None
    if args.project:
        pr = fsproj.load(args.project)
        target_wd = fsproj.work_dir(pr)
        if not (target_wd / "edit.json").exists():
            sys.exit("这个工程还没粗剪过（没有 edit.json），请先运行 transcribe.py 和 analyze.py auto")
        if not (target_wd / "project_path.txt").exists():
            (target_wd / "project_path.txt").write_text(str(pr.dir), "utf-8")
    port = ensure_running()
    url = project_url(port, target_wd) if target_wd else f"http://127.0.0.1:{port}/"
    print(f"READY {url}", flush=True)
    if not args.no_open:
        import webbrowser
        webbrowser.open(url)


if __name__ == "__main__":
    main()
