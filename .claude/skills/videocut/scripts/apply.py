# /// script
# requires-python = ">=3.10"
# ///
"""把 edit.json 写回 FocuSee 工程：playTracks（剪辑）+ subtitles.items（字幕）。

用法：
  uv run apply.py --project "<xxx.focusee>" [--dry-run] [--force] [--no-subtitles]

--dry-run 不写工程，只输出汇总，并在工作目录生成 subtitles.preview.srt 供检查断句。
写入前会把 configure.focuseeproj 备份成 configure.focuseeproj.bak-时间戳。
检测到 FocuSee 正在运行时默认拒绝写入（退出码 3），确认工程没在 FocuSee 里打开后可加 --force。
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze  # noqa: E402
import fsproj  # noqa: E402
import subtitles  # noqa: E402


SCREEN_INSET = 1.0  # "screen" 位置：字幕底边比录屏窗口下边缘再往里收多少（画布高度 %）


class FocuSeeRunning(RuntimeError):
    pass


def to_srt(items: list[dict]) -> str:
    def ts(t):
        ms = int(round(t * 1000))
        h, ms = divmod(ms, 3600000)
        m, ms = divmod(ms, 60000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    return "\n".join(f"{i}\n{ts(x['start'])} --> {ts(x['end'])}\n{x['text']}\n" for i, x in enumerate(items, 1))


def compute(pr, wd, edit: dict, transcript: dict, want_subs: bool = True):
    """返回 (keeps, playTracks, subtitle items)，不写任何文件。"""
    D = pr.duration
    params = analyze.params_of(edit)
    keeps = analyze.current_keeps(edit, D)
    template = (pr.conf.get("playTracks") or [None])[0]
    tracks = fsproj.keeps_to_playtracks(keeps, D, template, params["min_keep"])
    items = []
    if want_subs:
        keeps_now = fsproj.playtracks_to_keeps(tracks, D)
        items = subtitles.build({**transcript, "duration": D}, keeps_now, params,
                                edit.get("replacements", []), edit.get("sentence_text", {}))
    return keeps, tracks, items


def apply_edit(project_arg, edit: dict | None = None, force: bool = False, dry_run: bool = False,
               subtitles_on: bool | None = None) -> dict:
    pr = fsproj.load(project_arg)
    wd = fsproj.work_dir(pr)
    fsproj.ensure_original(pr, wd)
    if edit is None:
        edit = analyze.load_edit(wd)
        if edit is None:
            raise FileNotFoundError("还没有 edit.json，请先运行 analyze.py auto")
    transcript = json.loads((wd / "transcript.json").read_text("utf-8"))
    D = pr.duration
    params = analyze.params_of(edit)
    want_subs = edit.get("subtitles", {}).get("enabled", True) if subtitles_on is None else subtitles_on
    keeps, tracks, items = compute(pr, wd, edit, transcript, want_subs)

    conf = copy.deepcopy(pr.conf)
    conf["playTracks"] = tracks
    if want_subs:
        subs = conf.setdefault("subtitles", {})
        subs["items"] = items
        subs["isEnable"] = True
        style = subs.setdefault("pageStyle", {"bottomPadding": 5.0, "upPadding": 5.0, "fontSize": 18.0, "aligment": 0})
        for k, v in (params.get("subtitle_style") or {}).items():
            if v == "screen":
                v = round(fsproj.screen_bottom_margin(pr) + SCREEN_INSET, 1)
            style[k] = float(v) if isinstance(v, (int, float)) and k != "aligment" else v
        subs["language"] = subs.get("language") or "mandarinSimplified"

    result = analyze.summary(edit, D)
    result["subtitle_lines"] = len(items)
    result["project"] = str(pr.dir)
    warns = analyze.review_warnings(edit, transcript, D)
    if warns:
        result["review"] = warns
    if dry_run:
        if items:
            (wd / "subtitles.preview.srt").write_text(to_srt(items), "utf-8")
            result["subtitles_preview"] = str(wd / "subtitles.preview.srt")
        result["dry_run"] = True
        return result

    running = fsproj.focusee_running()
    if running and not force:
        raise FocuSeeRunning("检测到 FocuSee 正在运行（" + ", ".join(running) + "）。"
                             "请先在 FocuSee 里关闭这个工程（最好退出 FocuSee），否则它可能用旧状态覆盖写入。")
    bak = fsproj.save(pr, conf, backup=True)
    (wd / "edit.json").write_text(json.dumps(edit, ensure_ascii=False, indent=1), "utf-8")
    if items:
        (wd / "subtitles.srt").write_text(to_srt(items), "utf-8")
    result["backup"] = str(bak) if bak else None
    return result


def main():
    fsproj.utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="FocuSee 在运行也写入（确认没打开这个工程时用）")
    ap.add_argument("--no-subtitles", action="store_true", help="不写字幕轨")
    args = ap.parse_args()
    try:
        res = apply_edit(args.project, force=args.force, dry_run=args.dry_run,
                         subtitles_on=False if args.no_subtitles else None)
    except FocuSeeRunning as e:
        print(str(e), file=sys.stderr)
        sys.exit(3)
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
