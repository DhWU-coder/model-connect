"""通过真实子进程验证前后台服务生命周期及误停止防护。"""

import json
import os
import socket
import subprocess
import sys
import time

import httpx
import psutil
import pytest

from model_connect.cli import managed_process


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def command(env, *args):
    return subprocess.run(
        [sys.executable, "-m", "model_connect", *args],
        env=env,
        text=True,
        capture_output=True,
        timeout=25,
    )


def wait_health(port):
    for _ in range(80):
        try:
            response = httpx.get(
                f"http://127.0.0.1:{port}/api/health", trust_env=False, timeout=0.2
            )
            if response.is_success:
                return response.json()
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise AssertionError("服务未启动")


def test_background_restart_stop_end(tmp_path):
    env = {**os.environ, "MODEL_CONNECT_STATE_DIR": str(tmp_path)}
    port = free_port()
    try:
        start = command(env, "start", "--port", str(port))
        assert start.returncode == 0, start.stderr
        first = wait_health(port)
        duplicate = command(env, "start", "--port", str(port))
        assert "已经运行" in duplicate.stdout
        assert wait_health(port)["pid"] == first["pid"]
        assert "运行中" in command(env, "status").stdout
        restart = command(env, "restart")
        assert restart.returncode == 0, restart.stderr
        second = wait_health(port)
        assert first["instance"] != second["instance"]
        assert "已停止" in command(env, "end").stdout
        assert "未运行" in command(env, "status").stdout
        assert "未运行" in command(env, "stop").stdout
        assert "Model Connect" in command(env, "logs").stdout
    finally:
        command(env, "stop")


def test_foreground_and_port_conflict(tmp_path):
    env = {**os.environ, "MODEL_CONNECT_STATE_DIR": str(tmp_path)}
    port = free_port()
    with (tmp_path / "foreground.log").open("w") as output:
        foreground = subprocess.Popen(
            [sys.executable, "-m", "model_connect", "run", "--port", str(port)],
            env=env,
            stdout=output,
            stderr=output,
        )
        try:
            wait_health(port)
            assert "运行中" in command(env, "status").stdout
            assert "已停止" in command(env, "stop").stdout
            foreground.wait(timeout=5)
        finally:
            if foreground.poll() is None:
                foreground.terminate()
                foreground.wait(timeout=5)
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", port))
        occupied.listen()
        result = command(env, "start", "--port", str(port))
        assert result.returncode == 1
        assert "占用" in result.stderr
    assert command(env, "run", "--port", "0").returncode == 1


def test_reused_pid_not_signalled(tmp_path):
    process = psutil.Process()
    state = {
        "pid": process.pid,
        "created": process.create_time() - 10,
        "host": "127.0.0.1",
        "port": 8765,
        "instance": "stale",
    }
    assert managed_process(state) is None
    (tmp_path / "service.json").write_text(json.dumps(state))
    result = command({**os.environ, "MODEL_CONNECT_STATE_DIR": str(tmp_path)}, "stop")
    assert result.returncode == 0
    assert process.is_running()
    assert not (tmp_path / "service.json").exists()


@pytest.mark.parametrize("command_name", ["start", "restart", "run"])
def test_invalid_port(command_name, tmp_path):
    env = {**os.environ, "MODEL_CONNECT_STATE_DIR": str(tmp_path)}
    result = command(env, command_name, "--port", "99999")
    assert result.returncode == 1
    assert "端口" in result.stderr
