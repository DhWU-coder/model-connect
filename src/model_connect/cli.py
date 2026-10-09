"""管理本地服务的前台、后台与重启生命周期。"""

import argparse
import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
import psutil
import uvicorn

from model_connect import __version__
from model_connect.app import create_app


def state_dir() -> Path:
    override = os.environ.get("MODEL_CONNECT_STATE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "model-connect"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "model-connect"
    return (
        Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
        / "model-connect"
    )


def prepare_dir() -> Path:
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


@contextlib.contextmanager
def file_lock(name: str):
    """用操作系统文件锁避免不同 CLI 同时修改进程状态。"""
    file = (prepare_dir() / name).open("a+b")
    try:
        try:
            if os.name == "nt":
                import msvcrt

                file.seek(0)
                file.write(b"0")
                file.flush()
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            raise RuntimeError("已有服务或管理命令正在运行，请稍后重试") from exc
        yield
    finally:
        # 关闭文件描述符会释放锁，即使进程被意外终止也能恢复。
        file.close()


def read_state() -> dict | None:
    try:
        data = json.loads((state_dir() / "service.json").read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not all(
            key in data for key in ("pid", "created", "host", "port", "instance")
        ):
            return None
        return data
    except (OSError, ValueError):
        return None


def managed_process(data: dict | None) -> psutil.Process | None:
    """同时核对 PID、进程创建时间与命令，避免 PID 重用误停止。"""
    if not data:
        return None
    try:
        process = psutil.Process(int(data["pid"]))
        if abs(process.create_time() - float(data["created"])) > 0.01:
            return None
        if process.status() == psutil.STATUS_ZOMBIE:
            return None
        command = " ".join(process.cmdline())
        if "model_connect" not in command and "model-connect" not in command:
            return None
        return process
    except (psutil.Error, TypeError, ValueError, KeyError):
        return None


def service_url(data: dict) -> str:
    host = data["host"]
    if host == "0.0.0.0":
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{data['port']}"


def healthy(data: dict) -> bool:
    try:
        response = httpx.get(service_url(data) + "/api/health", timeout=0.5, trust_env=False)
        content = response.json()
        return (
            response.is_success
            and content.get("service") == "model-connect"
            and content.get("instance") == data["instance"]
            and content.get("pid") == data["pid"]
        )
    except (httpx.HTTPError, ValueError):
        return False


def ensure_port(host: str, port: int) -> None:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))
    except OSError as exc:
        raise RuntimeError(f"无法监听 {host}:{port}，地址无效或端口已被占用") from exc


def clear_state(instance: str) -> None:
    data = read_state()
    if data and data["instance"] == instance:
        (state_dir() / "service.json").unlink(missing_ok=True)


def serve(host: str, port: int, instance: str | None = None) -> None:
    with file_lock("service.lock"):
        old = read_state()
        if managed_process(old):
            raise RuntimeError(f"服务已经运行：{service_url(old)}")
        ensure_port(host, port)
        instance = instance or uuid4().hex
        os.environ["MODEL_CONNECT_INSTANCE"] = instance
        process = psutil.Process()
        data = {
            "pid": process.pid,
            "created": process.create_time(),
            "host": host,
            "port": port,
            "instance": instance,
        }
        directory = prepare_dir()
        temporary = directory / f"service-{instance}.tmp"
        temporary.write_text(json.dumps(data), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(directory / "service.json")
        try:
            print(f"Model Connect 已启动：{service_url(data)}", flush=True)
            uvicorn.run(
                create_app(host=host), host=host, port=port, log_level="info", access_log=False
            )
        finally:
            clear_state(instance)


def start(host: str, port: int) -> None:
    data = read_state()
    if managed_process(data):
        print(f"服务已经运行：{service_url(data)}")
        return
    ensure_port(host, port)
    directory = prepare_dir()
    logfile = directory / "service.log"
    # 每次后台启动保留上次日志，防止单个文件无限增长。
    if logfile.exists() and logfile.stat().st_size > 2 * 1024 * 1024:
        logfile.replace(directory / "service.previous.log")
    instance = uuid4().hex
    command = [
        sys.executable,
        "-m",
        "model_connect",
        "_serve",
        "--host",
        host,
        "--port",
        str(port),
        "--instance",
        instance,
    ]
    kwargs = (
        {"start_new_session": True}
        if os.name != "nt"
        else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
    )
    with logfile.open("ab") as output:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
            close_fds=True,
            **kwargs,
        )
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        data = read_state()
        if data and data["instance"] == instance and healthy(data):
            print(f"后台服务已启动：{service_url(data)}\nPID：{data['pid']}\n日志：{logfile}")
            return
        if process.poll() is not None:
            raise RuntimeError(f"后台服务启动失败，请查看日志：{logfile}")
        time.sleep(0.1)
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    clear_state(instance)
    raise RuntimeError(f"后台服务未在 12 秒内就绪，请查看日志：{logfile}")


def stop() -> None:
    data = read_state()
    process = managed_process(data)
    if not process:
        if data:
            clear_state(data["instance"])
        print("服务未运行")
        return
    try:
        process.terminate()
        try:
            process.wait(timeout=10)
        except psutil.TimeoutExpired:
            if managed_process(data):
                process.kill()
                process.wait(timeout=5)
    except psutil.NoSuchProcess:
        pass
    clear_state(data["instance"])
    print("服务已停止")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="model-connect", description="通过真实调用检测模型连通性，提供本地中文 UI"
    )
    root.add_argument("--version", action="version", version=f"model-connect {__version__}")
    commands = root.add_subparsers(dest="command", required=True, metavar="命令")
    for name, description in (
        ("start", "后台启动"),
        ("restart", "重启后台服务"),
        ("run", "前台运行"),
    ):
        child = commands.add_parser(name, help=description)
        child.add_argument("--host", default=None, help="监听地址，默认 127.0.0.1")
        child.add_argument("--port", type=int, default=None, help="监听端口，默认 8765")
    commands.add_parser("stop", help="停止服务")
    commands.add_parser("end", help="停止服务，与 stop 相同")
    commands.add_parser("status", help="查看运行状态")
    logs = commands.add_parser("logs", help="显示后台日志")
    logs.add_argument("--lines", type=int, default=50, help="显示最后几行，默认 50")
    return root


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "_serve":
        # 后台子进程入口独立解析，不显示在用户命令列表里。
        internal = argparse.ArgumentParser()
        internal.add_argument("--host", required=True)
        internal.add_argument("--port", type=int, required=True)
        internal.add_argument("--instance", required=True)
        args = internal.parse_args(sys.argv[2:])
        args.command = "_serve"
    else:
        args = parser().parse_args()
    try:
        if args.command == "_serve":
            serve(args.host or "127.0.0.1", args.port or 8765, args.instance)
            return
        if args.command == "run":
            if args.port is not None and not 1 <= args.port <= 65535:
                raise RuntimeError("端口必须介于 1 和 65535 之间")
            serve(args.host or "127.0.0.1", args.port or 8765)
            return
        if args.command == "status":
            data = read_state()
            if managed_process(data):
                state = "运行中" if healthy(data) else "进程存在，但服务尚未就绪"
                print(
                    f"{state}：{service_url(data)}\nPID：{data['pid']}\n"
                    f"日志：{state_dir() / 'service.log'}"
                )
            else:
                print("服务未运行")
            return
        if args.command == "logs":
            if args.lines < 1:
                raise RuntimeError("日志行数必须大于 0")
            logfile = state_dir() / "service.log"
            if logfile.exists():
                print(
                    "\n".join(
                        logfile.read_text(encoding="utf-8", errors="replace").splitlines()[
                            -args.lines :
                        ]
                    )
                )
            else:
                print("暂无后台日志")
            return
        with file_lock("control.lock"):
            if args.command in {"stop", "end"}:
                stop()
                return
            previous = read_state() if args.command == "restart" else None
            host = args.host or (previous["host"] if previous else "127.0.0.1")
            port = args.port if args.port is not None else (previous["port"] if previous else 8765)
            if not 1 <= port <= 65535:
                raise RuntimeError("端口必须介于 1 和 65535 之间")
            if args.command == "restart":
                stop()
            start(host, port)
    except (RuntimeError, OSError, psutil.Error) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
