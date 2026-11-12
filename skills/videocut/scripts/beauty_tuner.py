# /// script
# requires-python = ">=3.10,<3.13"
# dependencies = ["mediapipe==0.10.21", "av>=12"]
# ///
"""美颜调参页：用工程里的真实摄像头画面，拖滑块实时看美颜效果，满意了保存成用户预设。

保存位置 %LOCALAPPDATA%/videocut/beauty_preset.json，之后 beauty.py 默认就用它（agent 也直接读这个文件）。
页面只在本机打开（127.0.0.1），画面不会上传到任何地方。

用法：uv run beauty_tuner.py --project "<xxx.focusee>" [--port 8766] [--no-open]
启动后输出 READY http://127.0.0.1:<port>/
"""
from __future__ import annotations

import argparse
import base64
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import beauty  # noqa: E402
import fsproj  # noqa: E402

PAGE = HERE.parent / "assets" / "beauty_tuner.html"
LOCK = threading.Lock()  # mediapipe / 美颜对象不是线程安全的


class Frames:
    """按时间取摄像头原始画面（美颜前），找好人脸关键点后缓存。"""

    def __init__(self, src: Path):
        import av
        self.src = src
        with av.open(str(src)) as c:
            v = c.streams.video[0]
            self.duration = float(v.duration * v.time_base) if v.duration else float(c.duration or 0) / 1e6
        self.cache: dict[float, tuple[np.ndarray, np.ndarray | None]] = {}
        self.finder = beauty.Beautifier({}, static=True)

    def get(self, t: float):
        t = round(max(0.0, min(self.duration - 0.1, t)), 1)
        if t in self.cache:
            return self.cache[t]
        import av
        img = None
        with av.open(str(self.src)) as c:
            v = c.streams.video[0]
            c.seek(int(max(0, t - 1) / v.time_base), stream=v)
            for frame in c.decode(v):
                img = frame.to_ndarray(format="bgr24")
                if frame.pts is not None and float(frame.pts * v.time_base) >= t:
                    break
        if img is None:
            raise ValueError(f"{t}s 取不到画面")
        self.finder.prev = None
        pts = self.finder.landmarks(img)
        if len(self.cache) > 12:
            self.cache.pop(next(iter(self.cache)))
        self.cache[t] = (img, pts)
        return img, pts


def crop_box(img: np.ndarray, pts: np.ndarray | None, mode: str) -> tuple[int, int, int, int]:
    H, W = img.shape[:2]
    if mode == "full" or pts is None:
        return 0, 0, W, H
    x0, y0 = pts.min(0)
    x1, y1 = pts.max(0)
    fw, fh = x1 - x0, y1 - y0
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    half = max(fw, fh) * 0.85
    a, b = int(max(0, cx - half)), int(min(W, cx + half))
    c, d = int(max(0, cy - half * 1.05)), int(min(H, cy + half * 0.95))
    return a, c, b, d


def jpeg(img: np.ndarray, max_side: int = 1100) -> str:
    h, w = img.shape[:2]
    s = min(1.0, max_side / max(h, w))
    if s < 1:
        img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii")


class Handler(BaseHTTPRequestHandler):
    frames: Frames
    renderer: beauty.Beautifier
    project: str = ""

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        if path == "/api/info":
            current = beauty.load_preset(None)
            default = json.loads(beauty.PRESET_FILE.read_text("utf-8"))
            user = beauty.user_preset_path()
            return self._json(200, {
                "project": self.project,
                "source": self.frames.src.name,
                "duration": self.frames.duration,
                "params": beauty.PARAMS,
                "current": {k: current.get(k, 0) for k in beauty.PARAMS},
                "default": {k: default.get(k, 0) for k in beauty.PARAMS},
                "preset_file": str(user),
                "has_user_preset": user.exists(),
            })
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if self.headers.get("Origin") not in (None, f"http://127.0.0.1:{self.server.server_port}",
                                              f"http://localhost:{self.server.server_port}"):
            return self._json(403, {"error": "forbidden origin"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception as e:
            return self._json(400, {"error": str(e)})
        params = {k: max(0, min(100, int(round(float((body.get("params") or {}).get(k, 0)))))) for k in beauty.PARAMS}
        if path == "/api/render":
            t0 = time.time()
            try:
                with LOCK:
                    img, pts = self.frames.get(float(body.get("t", 0)))
                    self.renderer.set_params(params)
                    out = self.renderer.process(img, pts.copy() if pts is not None else None)
                    a, c, b, d = crop_box(img, pts, body.get("view", "face"))
                    before, after = img[c:d, a:b], out[c:d, a:b]
                    if body.get("mirror"):  # 只是预览像照镜子一样左右翻转，不影响工程
                        before, after = cv2.flip(before, 1), cv2.flip(after, 1)
                    res = {"before": jpeg(before), "after": jpeg(after), "face": pts is not None}
                res["ms"] = int((time.time() - t0) * 1000)
                return self._json(200, res)
            except Exception as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        if path == "/api/save":
            p = beauty.user_preset_path()
            if p.name != "beauty_preset.json":  # 旧文件名的预设迁到新名字
                p = p.with_name("beauty_preset.json")
            data = {"name": str(body.get("name") or "我的预设"), **params,
                    "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}
            p.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
            return self._json(200, {"saved": str(p), "preset": data})
        return self._json(404, {"error": "not found"})


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
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()
    pr = fsproj.load(args.project)
    cam = beauty.webcam_file(pr)
    orig = beauty.original_of(cam)
    src = orig if orig.exists() else cam  # 总是用美颜前的原始画面
    if not src.exists():
        sys.exit(f"这个工程没有摄像头录像：{cam}")
    Handler.frames = Frames(src)
    Handler.renderer = beauty.Beautifier({}, static=True)
    Handler.project = str(pr.dir)
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
