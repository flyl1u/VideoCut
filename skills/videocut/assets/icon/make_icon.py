# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow>=10"]
# ///
"""生成 VideoCut 图标：黄色圆角方块 + 被一刀斜着剪开的播放三角（「剪视频」）。

输出同目录下的 videocut.svg（网页 favicon）、videocut.ico（桌面快捷方式）、videocut-256.png（预览）。
配色沿用 README 首图：黄 #F4D786、墨色 #343932。
用法：uv run make_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
BG, INK = "#F4D786", "#343932"

# 几何都按 256 的画布写，SVG 和位图共用
RADIUS = 60
TRI = [(92, 66), (92, 190), (200, 128)]   # 播放三角（视觉上略往右放，看起来才居中）
CUT = [(164, 50), (116, 206)]              # 斜着的一刀
CUT_W = 12                                 # 刀口宽度
SHIFT = (4, -2)                            # 刀口右上那一块稍微错开，像被剪开


def side(p, a, b):
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


def split_triangle():
    """把三角形沿刀线分成两块多边形（各自是凸多边形）。"""
    a, b = CUT
    pts = TRI + [TRI[0]]
    left, right = [], []
    for p, q in zip(pts, pts[1:]):
        sp, sq = side(p, a, b), side(q, a, b)
        (left if sp >= 0 else right).append(p)
        if sp * sq < 0:
            t = sp / (sp - sq)
            x = (p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1]))
            left.append(x)
            right.append(x)
    return left, right


def shift(poly, d):
    return [(x + d[0], y + d[1]) for x, y in poly]


def pieces():
    left, right = split_triangle()
    # 判断哪块在刀线右上方：它的点离 (200,128) 更近
    if min(abs(x - 200) + abs(y - 128) for x, y in left) < min(abs(x - 200) + abs(y - 128) for x, y in right):
        left, right = right, left
    return left, shift(right, SHIFT)


def render(size: int) -> Image.Image:
    s = 4 * size / 256  # 4 倍超采样
    big = Image.new("RGBA", (4 * size, 4 * size), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    d.rounded_rectangle([0, 0, 4 * size - 1, 4 * size - 1], radius=RADIUS * s, fill=BG)
    for poly in pieces():
        d.polygon([(x * s, y * s) for x, y in poly], fill=INK)
    # 刀口：用底色画一条粗线，把两块分开
    (x0, y0), (x1, y1) = CUT
    d.line([(x0 * s, y0 * s), (x1 * s, y1 * s)], fill=BG, width=round(CUT_W * s))
    return big.resize((size, size), Image.LANCZOS)


def svg() -> str:
    a, b = pieces()
    poly = lambda ps: " ".join(f"{x:.1f},{y:.1f}" for x, y in ps)
    (x0, y0), (x1, y1) = CUT
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256">\n'
        f'  <rect width="256" height="256" rx="{RADIUS}" fill="{BG}"/>\n'
        f'  <polygon points="{poly(a)}" fill="{INK}"/>\n'
        f'  <polygon points="{poly(b)}" fill="{INK}"/>\n'
        f'  <line x1="{x0}" y1="{y0}" x2="{x1}" y2="{y1}" stroke="{BG}" stroke-width="{CUT_W}"/>\n'
        '</svg>\n'
    )


def main():
    (HERE / "videocut.svg").write_text(svg(), "utf-8")
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    imgs = {n: render(n) for n in sizes}
    imgs[256].save(HERE / "videocut.ico", sizes=[(n, n) for n in sizes],
                   append_images=[imgs[n] for n in sizes[:-1]])
    imgs[256].save(HERE / "videocut-256.png")
    print("ok", HERE)


if __name__ == "__main__":
    main()
