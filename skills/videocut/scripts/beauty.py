# /// script
# requires-python = ">=3.10,<3.13"
# dependencies = ["mediapipe==0.10.21", "av>=12"]
# ///
"""给 FocuSee 工程的摄像头画面加美颜（磨皮、祛斑、匀肤、美白、祛法令纹、小脸、窄脸）。

FocuSee 自己没有人脸美颜（它的 Beauty 是音频美化），所以直接处理 recording/webcam-0.mp4：
逐帧用 mediapipe 找人脸关键点，只在皮肤区域做处理，再按原时间戳重新编码，音画同步不变。
原文件改名为 webcam-0.original.mp4 留在原处，随时可以 --restore 还原；再次运行总是从原文件开始，不会叠加。

用法：
  uv run beauty.py --project "<xxx.focusee>" --preview 120,300   # 只出对比图，不改工程
  uv run beauty.py --project "<xxx.focusee>"                     # 处理整段并替换（耗时长，用 bg.py 放到后台）
  uv run beauty.py --project "<xxx.focusee>" --restore           # 换回原始摄像头画面

参数（0–100，含义同剪映）：默认读 assets/beauty_preset.json，
%USERPROFILE%/.videocut/beauty_preset.json 存在时以它为准（用户在调参页 beauty_tuner.py 保存的预设）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from fractions import Fraction
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fsproj  # noqa: E402

PRESET_FILE = HERE.parent / "assets" / "beauty_preset.json"

# mediapipe FaceMesh 关键点序号
FACE_OVAL = [10, 338, 297, 332, 284, 251, 389, 356, 454, 323, 361, 288, 397, 365, 379, 378, 400, 377, 152, 148,
             176, 149, 150, 136, 172, 58, 132, 93, 234, 127, 162, 21, 54, 103, 67, 109]
LEFT_EYE = [33, 7, 163, 144, 145, 153, 154, 155, 133, 173, 157, 158, 159, 160, 161, 246]
RIGHT_EYE = [362, 382, 381, 380, 374, 373, 390, 249, 263, 466, 388, 387, 386, 385, 384, 398]
LIPS = [61, 146, 91, 181, 84, 17, 314, 405, 321, 375, 291, 409, 270, 269, 267, 0, 37, 39, 40, 185]
LEFT_BROW = [70, 63, 105, 66, 107, 55, 65, 52, 53, 46]
RIGHT_BROW = [300, 293, 334, 296, 336, 285, 295, 282, 283, 276]
NOSTRILS = [98, 327, 2, 64, 294]
# 下颌轮廓（左 → 下巴 → 右），小脸/窄脸往里收的控制点
JAW = [234, 93, 132, 58, 172, 136, 150, 149, 176, 148, 152, 377, 400, 378, 379, 365, 397, 288, 361, 323, 454]
NOSE_TIP = 1
NOSE_WING = (129, 358)
MOUTH_CORNER = (61, 291)
UNDER_EYE = ((145, 23, 230), (374, 253, 450))


def user_preset_path() -> Path:
    """用户自己调好的参数（调参页保存到这里）；旧版本叫 beauty.json。"""
    root = fsproj.data_root()
    old = root / "beauty.json"
    new = root / "beauty_preset.json"
    return old if old.exists() and not new.exists() else new


def load_preset(path: str | None) -> dict:
    p = Path(path) if path else None
    if p is None:
        user = user_preset_path()
        p = user if user.exists() else PRESET_FILE
    data = json.loads(p.read_text("utf-8"))
    data["_file"] = str(p)
    return data


def webcam_file(pr: fsproj.Project) -> Path:
    rec = pr.dir / "recording"
    for r in pr.meta.get("recorders", []):
        if "cam" in str(r.get("type", "")).lower():
            fn = (r.get("sessions") or [{}])[0].get("outputFilename")
            if fn:
                return rec / fn
    return rec / "webcam-0.mp4"


def original_of(cam: Path) -> Path:
    return cam.with_name(cam.stem + ".original" + cam.suffix)


def odd(x: float, lo: int = 3) -> int:
    n = max(lo, int(round(x)))
    return n if n % 2 else n + 1


PARAMS = ["smooth", "whiten", "even_skin", "spots", "nasolabial", "dark_circles",
          "small_face", "thin_face", "narrow_face", "big_eyes"]


class Beautifier:
    def __init__(self, preset: dict, static: bool = False):
        import mediapipe as mp
        self.set_params(preset)
        self.mesh = mp.solutions.face_mesh.FaceMesh(static_image_mode=static, max_num_faces=1,
                                                    refine_landmarks=False, min_detection_confidence=0.5,
                                                    min_tracking_confidence=0.5)
        self.prev = None  # 平滑后的关键点，减少变形抖动
        self.faces = 0
        self.frames = 0
        self.has_guided = hasattr(cv2, "ximgproc")

    def set_params(self, preset: dict) -> None:
        self.p = {k: float(v) / 100.0 for k, v in preset.items() if isinstance(v, (int, float))}

    def get(self, k: str) -> float:
        return self.p.get(k, 0.0)

    # ------------------------------------------------------------ 关键点
    def landmarks(self, bgr: np.ndarray) -> np.ndarray | None:
        h, w = bgr.shape[:2]
        res = self.mesh.process(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
        if not res.multi_face_landmarks:
            self.prev = None
            return None
        pts = np.array([(l.x * w, l.y * h) for l in res.multi_face_landmarks[0].landmark], np.float32)
        if self.prev is not None:
            move = float(np.median(np.linalg.norm(pts - self.prev, axis=1)))
            a = 0.35 if move < 2.0 else 0.8  # 静止时多平滑，快速移动时跟紧
            pts = self.prev + (pts - self.prev) * a
        self.prev = pts
        return pts

    # ------------------------------------------------------------ 主处理
    def process(self, bgr: np.ndarray, pts: np.ndarray | None = None) -> np.ndarray:
        """pts 给了就直接用（调参页对同一帧反复渲染时不用每次重找关键点）。"""
        self.frames += 1
        if pts is None:
            pts = self.landmarks(bgr)
        if pts is None:
            return bgr
        self.faces += 1
        H, W = bgr.shape[:2]
        x0, y0 = pts.min(0)
        x1, y1 = pts.max(0)
        fw = float(x1 - x0)
        if fw < 40:
            return bgr
        m = 0.3 * fw
        rx0, ry0 = int(max(0, x0 - m)), int(max(0, y0 - m))
        rx1, ry1 = int(min(W, x1 + m)), int(min(H, y1 + m))
        roi = bgr[ry0:ry1, rx0:rx1]
        lp = pts - np.array([rx0, ry0], np.float32)
        out = self.skin(roi, lp, fw)
        out = self.reshape(out, lp, fw)
        out = self.enlarge_eyes(out, lp)
        res = bgr.copy()
        res[ry0:ry1, rx0:rx1] = out
        return res

    # ------------------------------------------------------------ 皮肤
    def masks(self, shape, lp: np.ndarray, fw: float) -> np.ndarray:
        h, w = shape[:2]
        mask = np.zeros((h, w), np.uint8)
        cv2.fillPoly(mask, [lp[FACE_OVAL].astype(np.int32)], 255)
        hole = np.zeros_like(mask)
        for idx in (LEFT_EYE, RIGHT_EYE, LIPS, LEFT_BROW, RIGHT_BROW):
            cv2.fillPoly(hole, [cv2.convexHull(lp[idx].astype(np.int32))], 255)
        for i in NOSTRILS:
            cv2.circle(hole, tuple(int(v) for v in lp[i]), max(2, int(fw * 0.035)), 255, -1)
        hole = cv2.dilate(hole, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (odd(fw * 0.05),) * 2))
        mask = cv2.subtract(mask, hole)
        return mask

    def skin(self, roi: np.ndarray, lp: np.ndarray, fw: float) -> np.ndarray:
        base = self.masks(roi.shape, lp, fw)
        # 肤色门限：避免把头发、胡子、背景当皮肤磨
        ycc = cv2.cvtColor(roi, cv2.COLOR_BGR2YCrCb)
        cr = ycc[..., 1].astype(np.float32)
        cb = ycc[..., 2].astype(np.float32)
        gate = np.clip((cr - 128) / 10, 0, 1) * np.clip((140 - cb) / 10, 0, 1)
        m = base.astype(np.float32) / 255 * gate
        m = cv2.GaussianBlur(m, (0, 0), max(1.0, fw * 0.025))
        m3 = m[..., None]
        img = roi.astype(np.float32)

        # 祛斑祛痘：比周围暗的小点，用周围的颜色补上
        if self.get("spots") > 0:
            k = odd(fw * 0.07, 5)
            med = cv2.medianBlur(roi, k).astype(np.float32)
            L = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY).astype(np.float32)
            Lm = cv2.cvtColor(med.astype(np.uint8), cv2.COLOR_BGR2GRAY).astype(np.float32)
            spot = np.clip((Lm - L - 5) / 10, 0, 1)
            big = cv2.morphologyEx(spot, cv2.MORPH_OPEN,
                                   cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (odd(fw * 0.06),) * 2))
            spot = np.clip(spot - big, 0, 1)  # 只留小点，大块阴影不动
            spot = cv2.GaussianBlur(cv2.dilate(spot, None), (0, 0), max(1.0, fw * 0.006))
            a = np.clip(spot * self.get("spots") * 1.5, 0, 1)[..., None] * m3
            img = img + (med - img) * a

        # 磨皮：保边平滑 + 保留一点细纹理，避免塑料感
        s = self.get("smooth")
        nl = self.get("nasolabial")
        if s > 0 or nl > 0:
            u8 = np.clip(img, 0, 255).astype(np.uint8)
            r = max(3, int(fw * 0.025))
            if self.has_guided:
                sm = cv2.ximgproc.guidedFilter(u8, u8, r, (0.025 * 255) ** 2).astype(np.float32)
            else:
                sm = cv2.bilateralFilter(u8, odd(r * 2), 30, r).astype(np.float32)
            a = np.full(m.shape, min(1.0, s * 2.2), np.float32)
            if nl > 0:  # 法令纹：鼻翼到嘴角外侧这一条额外加强
                band = np.zeros(m.shape, np.float32)
                for wing, corner in zip(NOSE_WING, MOUTH_CORNER):
                    p0, p1 = lp[wing], lp[corner]
                    c = lp[NOSE_TIP]
                    out_dir = np.sign(p0[0] - c[0])
                    off = np.array([out_dir * fw * 0.03, 0], np.float32)
                    cv2.line(band, tuple(int(v) for v in p0 + off), tuple(int(v) for v in p1 + off * 1.5), 1.0,
                             max(2, int(fw * 0.06)))
                band = cv2.GaussianBlur(band, (0, 0), max(1.0, fw * 0.03))
                a = np.clip(a + band * nl * 6, 0, 1)
            a3 = (a * m)[..., None]
            detail = img - cv2.GaussianBlur(img, (0, 0), 1.0)
            img = img + (sm - img) * a3 + detail * 0.35 * a3

        # 匀肤：肤色往周围平均色靠一点，消掉泛红和色块
        e = self.get("even_skin")
        if e > 0:
            lab = cv2.cvtColor(np.clip(img, 0, 255).astype(np.uint8), cv2.COLOR_BGR2LAB).astype(np.float32)
            blur = cv2.GaussianBlur(lab, (0, 0), max(2.0, fw * 0.08))
            k = np.clip(e * 3, 0, 1) * m
            lab[..., 1] += (blur[..., 1] - lab[..., 1]) * k
            lab[..., 2] += (blur[..., 2] - lab[..., 2]) * k
            img = cv2.cvtColor(np.clip(lab, 0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR).astype(np.float32)

        # 祛黑眼圈：眼睛下方一小块往周围（脸颊）的亮度提
        dc = self.get("dark_circles")
        if dc > 0:
            lab = cv2.cvtColor(np.clip(img, 0, 255).astype(np.uint8), cv2.COLOR_BGR2LAB).astype(np.float32)
            band = np.zeros(m.shape, np.float32)
            ew_all = []
            for eye in (LEFT_EYE, RIGHT_EYE):
                e_pts = lp[eye]
                c = e_pts.mean(0)
                ew = float(e_pts[:, 0].max() - e_pts[:, 0].min())
                ew_all.append(ew)
                cv2.ellipse(band, (int(c[0]), int(c[1] + ew * 0.42)), (int(ew * 0.55), int(ew * 0.22)), 0, 0, 360, 1.0, -1)
            ew = max(ew_all)
            band = cv2.GaussianBlur(band, (0, 0), max(1.0, ew * 0.18))
            ref = cv2.GaussianBlur(lab[..., 0], (0, 0), max(2.0, ew * 0.6))
            lift = np.clip(ref + 6 - lab[..., 0], 0, None)
            lab[..., 0] += lift * np.clip(dc * 1.6, 0, 1) * band
            img = cv2.cvtColor(np.clip(lab, 0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR).astype(np.float32)

        # 美白：提亮皮肤，越暗提得越多一点
        wv = self.get("whiten")
        if wv > 0:
            img = img + (255 - img) * (wv * 0.9) * m3

        return np.clip(img, 0, 255).astype(np.uint8)

    # ------------------------------------------------------------ 脸型
    def reshape(self, roi: np.ndarray, lp: np.ndarray, fw: float) -> np.ndarray:
        small, narrow, thin = self.get("small_face"), self.get("narrow_face"), self.get("thin_face")
        if small <= 0 and narrow <= 0 and thin <= 0:
            return roi
        h, w = roi.shape[:2]
        center = lp[NOSE_TIP]
        ctrl, disp = [], []
        n = len(JAW)
        for i, idx in enumerate(JAW):
            p = lp[idx]
            d = np.zeros(2, np.float32)
            d += (center - p) * small * 0.10                          # 小脸：下半张脸整体往鼻尖收
            d[0] += (center[0] - p[0]) * narrow * 0.10                # 窄脸：只横向收
            mid = 1 - abs(i - n // 2) / (n // 2)                      # 瘦脸：两颊多、下巴少
            d[0] += (center[0] - p[0]) * thin * 0.08 * (1 - mid)
            ctrl.append(p)
            disp.append(d)
        ctrl = np.array(ctrl, np.float32)
        disp = np.array(disp, np.float32)
        # 位移场在 1/4 分辨率上算，再放大，省时间
        s = 4
        gh, gw = (h + s - 1) // s, (w + s - 1) // s
        gy, gx = np.mgrid[0:gh, 0:gw].astype(np.float32) * s
        r = fw * 0.32
        acc_x = np.zeros((gh, gw), np.float32)
        acc_y = np.zeros((gh, gw), np.float32)
        acc_w = np.zeros((gh, gw), np.float32)
        tgt = ctrl + disp
        for c, d in zip(tgt, disp):
            d2 = (gx - c[0]) ** 2 + (gy - c[1]) ** 2
            wgt = np.clip(1 - d2 / (r * r), 0, 1) ** 2
            acc_x += wgt * d[0]
            acc_y += wgt * d[1]
            acc_w += wgt
        norm = np.maximum(acc_w, 1.0)
        dx = cv2.resize(acc_x / norm, (w, h), interpolation=cv2.INTER_LINEAR)
        dy = cv2.resize(acc_y / norm, (w, h), interpolation=cv2.INTER_LINEAR)
        # 眼睛和眉毛不跟着收：脸两侧的控制点和眼角差不多高，不保护的话会把眼睛横向挤小
        keep = np.zeros((h, w), np.uint8)
        for idx in (LEFT_EYE, RIGHT_EYE, LEFT_BROW, RIGHT_BROW):
            cv2.fillPoly(keep, [cv2.convexHull(lp[idx].astype(np.int32))], 255)
        keep = cv2.dilate(keep, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (odd(fw * 0.16),) * 2))
        keep = cv2.GaussianBlur(keep.astype(np.float32) / 255, (0, 0), max(1.0, fw * 0.06))
        dx *= 1 - keep
        dy *= 1 - keep
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        # 反向映射：输出像素 x 取原图 x - d 处
        return cv2.remap(roi, xx - dx, yy - dy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)

    def enlarge_eyes(self, roi: np.ndarray, lp: np.ndarray) -> np.ndarray:
        """大眼：以眼睛中心做局部放大，越靠中心放大越多，边缘平滑过渡到不变。"""
        k = self.get("big_eyes")
        if k <= 0:
            return roi
        h, w = roi.shape[:2]
        mx = np.zeros((h, w), np.float32)
        my = np.zeros((h, w), np.float32)
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        for eye in (LEFT_EYE, RIGHT_EYE):
            e_pts = lp[eye]
            c = e_pts.mean(0)
            R = float(e_pts[:, 0].max() - e_pts[:, 0].min()) * 0.95
            ddx, ddy = xx - c[0], yy - c[1]
            d2 = (ddx ** 2 + ddy ** 2) / (R * R)
            # 采样点往中心缩：s = 1 - a·(1 - d²)²，d≥1 时为 1（不变）
            s = 1 - k * 0.32 * np.clip(1 - d2, 0, 1) ** 2
            mx += ddx * (s - 1)
            my += ddy * (s - 1)
        return cv2.remap(roi, xx + mx, yy + my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)


# ---------------------------------------------------------------- 视频读写

def pick_encoder() -> tuple[str, dict]:
    import av
    avail = av.codecs_available
    if "h264_nvenc" in avail:
        return "h264_nvenc", {"preset": "p5", "rc": "vbr", "cq": "19", "b:v": "0"}
    return "libx264", {"crf": "17", "preset": "veryfast"}


def process_video(src: Path, dst: Path, bt: Beautifier, limit: tuple[float, float] | None = None,
                  progress=True) -> dict:
    import av
    inp = av.open(str(src))
    vin = inp.streams.video[0]
    vin.thread_type = "AUTO"
    tb = vin.time_base
    total = vin.frames or 0
    out = av.open(str(dst), "w")
    enc_name, opts = pick_encoder()
    for attempt in (enc_name, "libx264"):
        try:
            vout = out.add_stream(attempt, rate=Fraction(vin.average_rate or 60).limit_denominator(1000),
                                  options=opts if attempt == enc_name else {"crf": "17", "preset": "veryfast"})
            vout.width, vout.height = vin.codec_context.width, vin.codec_context.height
            vout.pix_fmt = "yuv420p"
            vout.time_base = tb
            vout.codec_context.time_base = tb
            vout.codec_context.open()
            enc_name = attempt
            break
        except Exception as e:  # noqa: BLE001
            print(f"编码器 {attempt} 不可用：{e}", file=sys.stderr)
            out.close()
            out = av.open(str(dst), "w")
    t0 = time.time()
    n = 0
    last = 0.0
    for frame in inp.decode(vin):
        t = float(frame.pts * tb) if frame.pts is not None else n / 60
        if limit and t < limit[0]:
            continue
        if limit and t > limit[1]:
            break
        img = frame.to_ndarray(format="bgr24")
        img = bt.process(img)
        nf = av.VideoFrame.from_ndarray(img, format="bgr24").reformat(format="yuv420p")
        nf.pts = frame.pts
        nf.time_base = tb
        for pkt in vout.encode(nf):
            out.mux(pkt)
        n += 1
        if progress and time.time() - last > 15:
            last = time.time()
            el = last - t0
            pct = n / total * 100 if total else 0
            eta = el / n * (total - n) if total and n else 0
            print(f"PROGRESS {n}/{total} {pct:.1f}% 已用 {el / 60:.1f} 分钟 预计还要 {eta / 60:.1f} 分钟", flush=True)
    for pkt in vout.encode(None):
        out.mux(pkt)
    out.close()
    inp.close()
    return {"frames": n, "seconds": round(time.time() - t0, 1), "encoder": enc_name,
            "face_frames": bt.faces}


def preview(src: Path, wd: Path, preset: dict, times: list[float]) -> list[str]:
    import av
    outs = []
    for t in times:
        bt = Beautifier(preset)
        inp = av.open(str(src))
        vin = inp.streams.video[0]
        inp.seek(int(max(0, t - 2) / vin.time_base), stream=vin)
        img = res = None
        for frame in inp.decode(vin):  # 先跑 2 秒让关键点跟踪稳定
            img = frame.to_ndarray(format="bgr24")
            res = bt.process(img)
            if float(frame.pts * vin.time_base) >= t:
                break
        inp.close()
        if img is None:
            continue
        pts = bt.prev
        if pts is not None:  # 裁出脸部放大，方便看清
            x0, y0 = pts.min(0)
            x1, y1 = pts.max(0)
            m = (x1 - x0) * 0.5
            H, W = img.shape[:2]
            a, b = int(max(0, x0 - m)), int(min(W, x1 + m))
            c, d = int(max(0, y0 - m)), int(min(H, y1 + m))
            img, res = img[c:d, a:b], res[c:d, a:b]
        both = np.hstack([img, res])
        p = wd / f"beauty_preview_{int(t)}.jpg"
        cv2.imencode(".jpg", both, [cv2.IMWRITE_JPEG_QUALITY, 92])[1].tofile(str(p))
        outs.append(str(p))
    return outs


def main():
    fsproj.utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project")
    ap.add_argument("--preset", help="美颜参数 JSON（默认用户预设或 assets/beauty_preset.json）")
    ap.add_argument("--preview", help="只生成这些秒数的前后对比图，逗号分隔，例如 60,300")
    ap.add_argument("--restore", action="store_true", help="换回原始摄像头画面")
    ap.add_argument("--force", action="store_true", help="FocuSee 窗口开着也替换（确认没打开这个工程时用）")
    args = ap.parse_args()

    pr = fsproj.load(args.project)
    wd = fsproj.work_dir(pr)
    cam = webcam_file(pr)
    orig = original_of(cam)
    src = orig if orig.exists() else cam
    if not src.exists():
        sys.exit(f"这个工程没有摄像头录像：{cam}")

    if args.restore:
        if not orig.exists():
            sys.exit("没有找到原始摄像头文件，摄像头画面本来就是原样")
        if fsproj.focusee_running() and not args.force:
            sys.exit(3)
        os.replace(orig, cam)
        (wd / "beauty.json").unlink(missing_ok=True)
        print(json.dumps({"restored": str(cam)}, ensure_ascii=False))
        return

    preset = load_preset(args.preset)
    if args.preview:
        times = [float(x) for x in args.preview.split(",") if x.strip()]
        print(json.dumps({"preview": preview(src, wd, preset, times), "preset": preset["_file"]},
                         ensure_ascii=False))
        return

    prev = wd / "beauty.json"
    if not orig.exists() and prev.exists() and json.loads(prev.read_text("utf-8")).get("inherited_from") and not args.force:
        sys.exit("这个工程是从美颜过的工程重拼来的（调序工程），摄像头已经美颜过，再做会叠加；确实要做加 --force")
    running = fsproj.focusee_running()
    if running and not args.force:
        print("FocuSee 窗口开着（" + ", ".join(running) + "），请先关掉这个工程再处理", file=sys.stderr)
        sys.exit(3)
    tmp = cam.with_name(cam.stem + ".beauty-tmp" + cam.suffix)
    print(f"源文件：{src.name}  预设：{preset.get('name', preset['_file'])}", flush=True)
    info = process_video(src, tmp, Beautifier(preset))
    if info["frames"] == 0:
        tmp.unlink(missing_ok=True)
        sys.exit("没有解码出任何帧，已放弃")
    if not orig.exists():
        os.replace(cam, orig)
    os.replace(tmp, cam)
    info.update({"webcam": str(cam), "original": str(orig), "preset": {k: v for k, v in preset.items()
                                                                        if not k.startswith("_")}})
    (wd / "beauty.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), "utf-8")
    print("DONE " + json.dumps(info, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
