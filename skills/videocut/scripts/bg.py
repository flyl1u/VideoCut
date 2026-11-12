# /// script
# requires-python = ">=3.10"
# ///
"""在后台跑耗时命令（美颜、预览视频、审阅服务……），任何 agent 都能用：不依赖 agent 自带的后台任务功能。

命令在独立进程里运行，输出写进日志文件；agent 的这条命令马上返回，之后随时查进度或停掉。
日志和状态在 %USERPROFILE%/.videocut/bg/<名字>.log / .json。

  uv run bg.py start <名字> [--until 文本] [--timeout 秒] -- <命令...>
      启动；给了 --until 就等到日志里出现这段文本（比如 READY）再返回，并打印那一行。
  uv run bg.py wait <名字> [--until 文本] [--timeout 秒]
      等到出现文本或命令结束（默认最多 600 秒），打印最后几行日志。
  uv run bg.py status [<名字>]       不给名字就列出全部
  uv run bg.py stop <名字>           连同子进程一起停掉

退出码：0 已出现文本 / 命令正常结束 / 仍在运行；1 命令出错退出；2 等待超时（命令还在跑）。
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fsproj  # noqa: E402

TAIL = 8


def bg_dir() -> Path:
    d = fsproj.data_root() / "bg"
    d.mkdir(parents=True, exist_ok=True)
    return d


def paths(name: str) -> tuple[Path, Path]:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    return bg_dir() / f"{safe}.json", bg_dir() / f"{safe}.log"


def read_state(name: str) -> dict | None:
    st, _ = paths(name)
    try:
        return json.loads(st.read_text("utf-8"))
    except (OSError, ValueError):
        return None


def write_state(name: str, data: dict) -> None:
    st, _ = paths(name)
    tmp = st.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    os.replace(tmp, st)


def alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def tail(log: Path, n: int = TAIL) -> list[str]:
    try:
        lines = log.read_bytes().decode("utf-8", "replace").replace("\r\n", "\n").split("\n")
    except OSError:
        return []
    # 进度条用 \r 覆盖同一行，只留最后一次
    lines = [ln.rsplit("\r", 1)[-1] for ln in lines if ln.strip()]
    return lines[-n:]


def find_line(log: Path, text: str) -> str | None:
    try:
        data = log.read_bytes().decode("utf-8", "replace")
    except OSError:
        return None
    for ln in data.splitlines():
        if text in ln:
            return ln.rsplit("\r", 1)[-1].strip()
    return None


def describe(name: str, st: dict) -> str:
    elapsed = int((st.get("ended") or time.time()) - st.get("started", time.time()))
    m, s = divmod(elapsed, 60)
    h, m = divmod(m, 60)
    dur = f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"
    if "exit" in st:
        state = f"EXITED code={st['exit']}"
    elif st.get("stopped"):
        state = "STOPPED"
    elif alive(st.get("pid", 0)):
        state = "RUNNING"
    else:
        state = "GONE（进程已不在，没有记录到退出码）"
    return f"{state} {name} 用时 {dur} pid={st.get('pid')} log={paths(name)[1]}"


# ---------------------------------------------------------------- 子命令

def cmd_run(name: str, argv: list[str]) -> int:
    """内部用：在独立进程里跑命令，把退出码记进状态文件。"""
    st_path, log = paths(name)
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    with open(log, "ab", buffering=0) as f:
        try:
            kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}  # 不弹黑窗
            p = subprocess.Popen(argv, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env, **kw)
        except OSError as e:
            f.write(f"\n[bg] 启动失败：{e}\n".encode("utf-8"))
            code = 127
        else:
            st = read_state(name) or {}
            st["child_pid"] = p.pid
            write_state(name, st)
            code = p.wait()
    st = read_state(name) or {}
    st.update(exit=code, ended=time.time())
    write_state(name, st)
    return code


def spawn(name: str, argv: list[str]) -> int:
    st_path, log = paths(name)
    old = read_state(name)
    if old and "exit" not in old and not old.get("stopped") and alive(old.get("pid", 0)):
        sys.exit(f"{name} 已经在运行（pid={old['pid']}）。先 `bg.py stop {name}`，或换个名字。")
    log.write_bytes(b"")
    write_state(name, {"name": name, "cmd": argv, "cwd": os.getcwd(), "started": time.time(), "log": str(log)})
    wrapper = [stable_python(), str(Path(__file__).resolve()), "_run", name, "--", *argv]
    pid = spawn_outside_agent(wrapper) if os.name == "nt" else None
    detached = pid is not None
    if pid is None:
        kw: dict = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        if os.name == "nt":
            p = subprocess.Popen(wrapper, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW, **kw)
        else:
            p = subprocess.Popen(wrapper, start_new_session=True, **kw)
        pid = p.pid
    st = read_state(name) or {}
    st.update(pid=pid, detached=detached)
    write_state(name, st)
    return pid


def stable_python() -> str:
    """不会被删掉的 Python。

    `uv run` 跑没有依赖的脚本时，sys.executable 是一个临时环境里的 python，命令结束后 uv 会把它删掉；
    后台进程要用它背后的基础解释器（uv 管理的 cpython），本 skill 的后台脚本只用标准库。
    """
    base = Path(getattr(sys, "_base_executable", "") or sys.executable)
    return str(base if base.exists() else Path(sys.executable))


def spawn_outside_agent(wrapper: list[str]) -> int | None:
    """Windows：让系统的 WMI 服务来创建进程。

    agent（Claude Code、Codex……）把自己启动的命令都放在一个作业对象里，agent 一退出就连带全部杀掉，
    而且不允许脱离（CREATE_BREAKAWAY_FROM_JOB 会被拒绝）。WMI 创建的进程不在这个作业里，agent 关掉也不受影响。
    用 pythonw 跑包装进程，不会弹出黑窗口。失败（比如沙箱不让用 WMI）返回 None，退回普通方式。
    """
    exe = Path(wrapper[0])
    pyw = exe.with_name("pythonw.exe")
    cmd = subprocess.list2cmdline([str(pyw if pyw.exists() else exe), *wrapper[1:]])
    ps = ("$si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0}; "
          "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
          "@{CommandLine=$env:VC_BG_CMD; CurrentDirectory=$env:VC_BG_CWD; ProcessStartupInformation=$si}; "
          "if ($r.ReturnValue -eq 0) { $r.ProcessId } else { exit 1 }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           env=dict(os.environ, VC_BG_CMD=cmd, VC_BG_CWD=os.getcwd()),
                           capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (r.stdout or "").strip().splitlines()
    return int(out[-1]) if r.returncode == 0 and out and out[-1].strip().isdigit() else None


def wait(name: str, until: str | None, timeout: float) -> int:
    _, log = paths(name)
    t_end = time.time() + timeout
    while True:
        st = read_state(name) or {}
        if until:
            hit = find_line(log, until)
            if hit is not None:
                print(f"MATCH {hit}")
                return 0
        done = "exit" in st or st.get("stopped") or not alive(st.get("pid", 0))
        if done:
            time.sleep(0.3)  # 让最后的输出落盘
            st = read_state(name) or st
            print(describe(name, st))
            for ln in tail(log):
                print("  " + ln)
            if until and find_line(log, until) is None:
                return 1
            return 0 if st.get("exit", 0) == 0 else 1
        if time.time() >= t_end:
            print(f"TIMEOUT 等了 {int(timeout)} 秒，{describe(name, st)}")
            for ln in tail(log):
                print("  " + ln)
            return 2
        time.sleep(0.5)


def kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            pass


def main() -> int:
    fsproj.utf8_stdout()
    if len(sys.argv) > 2 and sys.argv[1] == "_run":
        rest = sys.argv[3:]
        try:
            return cmd_run(sys.argv[2], rest[1:] if rest[:1] == ["--"] else rest)
        except BaseException:  # pythonw 没有控制台，出错时把原因留在日志里
            import traceback
            with open(paths(sys.argv[2])[1], "a", encoding="utf-8") as f:
                f.write("\n[bg] " + traceback.format_exc())
            raise

    argv = sys.argv[1:]
    command = argv[argv.index("--") + 1:] if "--" in argv else []
    argv = argv[:argv.index("--")] if "--" in argv else argv
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("start")
    a.add_argument("name")
    a.add_argument("--until")
    a.add_argument("--timeout", type=float, default=120)
    a = sub.add_parser("wait")
    a.add_argument("name")
    a.add_argument("--until")
    a.add_argument("--timeout", type=float, default=600)
    a = sub.add_parser("status")
    a.add_argument("name", nargs="?")
    a = sub.add_parser("stop")
    a.add_argument("name")
    args = ap.parse_args(argv)

    if args.cmd == "start":
        if not command:
            sys.exit("缺少要运行的命令：bg.py start <名字> -- <命令...>")
        pid = spawn(args.name, command)
        print(f"STARTED {args.name} pid={pid} log={paths(args.name)[1]}")
        if args.until:
            return wait(args.name, args.until, args.timeout)
        return 0
    if args.cmd == "wait":
        if read_state(args.name) is None:
            sys.exit(f"没有叫 {args.name} 的后台任务")
        return wait(args.name, args.until, args.timeout)
    if args.cmd == "status":
        names = [args.name] if args.name else sorted(p.stem for p in bg_dir().glob("*.json"))
        if not names:
            print("没有后台任务")
        for n in names:
            st = read_state(n)
            if st is None:
                print(f"没有叫 {n} 的后台任务")
                continue
            print(describe(n, st))
            if args.name:
                for ln in tail(paths(n)[1]):
                    print("  " + ln)
        return 0
    if args.cmd == "stop":
        st = read_state(args.name)
        if st is None:
            sys.exit(f"没有叫 {args.name} 的后台任务")
        for key in ("pid", "child_pid"):  # 先杀包装进程整棵树，避免它抢着写退出码
            if st.get(key) and alive(st[key]):
                kill_tree(st[key])
        st.update(stopped=True, ended=st.get("ended") or time.time())
        write_state(args.name, st)
        print(f"STOPPED {args.name}")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
