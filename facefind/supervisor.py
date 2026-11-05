"""托盘宿主进程：拉起 / 停止 / 重启 facefind UI 子进程。

进程模型：
  - 本进程（supervisor）常驻，负责托盘图标与生命周期管理；
  - UI 服务作为**子进程**运行（`python -m facefind ui --port N --no-browser`），
    子进程异常退出不会带走宿主，托盘菜单可以再次「启动服务」；
  - 「重启」= 优雅停子进程 → 等端口释放 → 重新拉起，避免两个进程抢同一端口；
  - 「退出」= 停子进程（触发 uvicorn 优雅关闭，watcher 线程与 Playwright 实例随之释放）
    → 移除托盘图标 → 宿主退出。

通信：
  - 宿主在 127.0.0.1 的随机端口上开一个极小的控制通道（NDJSON + token）；
  - 控制信息写入 `data/supervisor_<port>.json`，子进程通过环境变量
    `FACEINFO_SUPERVISOR_FILE` 找到它，从而让 UI 的「重启/退出」按钮驱动宿主。

单实例：
  - 每端口一个命名互斥体 `Local\\facefind_supervisor_<port>`；
  - 重复启动时若已有宿主，则向它发送 `open` 命令（打开浏览器）后自身退出。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
LOG_DIR = DATA / "logs"

HOST = "127.0.0.1"
EXE = sys.executable

STATE_RUNNING = "running"
STATE_STOPPED = "stopped"
STATE_STARTING = "starting"
STATE_ERROR = "error"
STATE_EXTERNAL = "external"      # 端口上已有别人起的 facefind 实例，复用但不托管

STATE_CN = {
    STATE_RUNNING: "运行中",
    STATE_STOPPED: "已停止",
    STATE_STARTING: "启动中",
    STATE_ERROR: "出错",
    STATE_EXTERNAL: "复用现有实例",
}

COLORS = {
    STATE_RUNNING: (34, 160, 90, 255),
    STATE_STOPPED: (140, 146, 156, 255),
    STATE_STARTING: (224, 150, 33, 255),
    STATE_ERROR: (214, 59, 59, 255),
    STATE_EXTERNAL: (56, 116, 214, 255),
}

_log = logging.getLogger("facefind.supervisor")


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _port_free(port: int, host: str = HOST) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _facefind_running(port: int, host: str = HOST) -> bool:
    """端口上是否已有本工具的 UI 实例。"""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/state", timeout=1.5) as resp:
            data = json.load(resp)
        return data.get("app") == "facefind"
    except Exception:
        return False


def _job_busy(port: int, host: str = HOST) -> bool:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/job", timeout=1.0) as resp:
            data = json.load(resp)
        return bool(data.get("busy"))
    except Exception:
        return False


def _post_admin(port: int, path: str, token: str, payload: dict | None = None,
                timeout: float = 3.0) -> bool:
    """调用子进程的内部管理接口（带 token）。"""
    import urllib.request

    req = urllib.request.Request(
        f"http://{HOST}:{port}{path}",
        data=json.dumps(payload or {}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "X-Facefind-Admin": token},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


class _Mutex:
    """Windows 命名互斥体；非 Windows 退化为进程内锁（仅用于开发环境）。"""

    def __init__(self, name: str):
        self.name = name
        self.handle = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        kernel32 = ctypes.windll.kernel32
        self.handle = kernel32.CreateMutexW(None, False, self.name)
        if kernel32.GetLastError() == 183:      # ERROR_ALREADY_EXISTS
            return False
        return True

    def release(self) -> None:
        if self.handle:
            try:
                ctypes.windll.kernel32.CloseHandle(self.handle)
            except Exception:
                pass
            self.handle = None


# --------------------------------------------------------------------------
# 托盘图标绘制
# --------------------------------------------------------------------------
def _icon_image(state: str):
    from PIL import Image, ImageDraw, ImageFont

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = COLORS.get(state, COLORS[STATE_STOPPED])
    d.ellipse((5, 5, 59, 59), fill=color, outline=(255, 255, 255, 210), width=2)
    try:
        font = ImageFont.load_default(size=30)
    except Exception:      # Pillow < 10
        font = ImageFont.load_default()
    d.text((size / 2, size / 2 - 1), "F", fill=(255, 255, 255, 255), anchor="mm", font=font)
    # 右下角状态角标
    badge = {
        STATE_RUNNING: (34, 160, 90, 255),
        STATE_STOPPED: (140, 146, 156, 255),
        STATE_STARTING: (224, 150, 33, 255),
        STATE_ERROR: (214, 59, 59, 255),
        STATE_EXTERNAL: (56, 116, 214, 255),
    }[state]
    d.ellipse((44, 44, 61, 61), fill=badge, outline=(255, 255, 255, 255), width=2)
    return img


# --------------------------------------------------------------------------
# 宿主
# --------------------------------------------------------------------------
class Supervisor:
    def __init__(self, port: int, config: str, extra: list[str], open_browser: bool = True):
        self.port = int(port)
        self.config = config
        self.extra = list(extra)
        self.open_browser = open_browser
        self.url = f"http://{HOST}:{self.port}/"
        self.token = secrets.token_hex(16)
        self.state = STATE_STOPPED
        self.reason = ""
        self.proc: subprocess.Popen | None = None
        self.busy = False
        self.control_port = 0
        self.state_file = DATA / f"supervisor_{self.port}.json"
        self.control_server: socket.socket | None = None
        self.stop_event = threading.Event()
        self.icon = None
        self.pending = ""            # restart / shutdown
        self._lock = threading.RLock()

    # ---------- 状态与呈现 ----------
    def _set_state(self, state: str, reason: str = "") -> None:
        with self._lock:
            self.state = state
            self.reason = reason
        self._write_state_file()
        self._refresh_tray()
        _log.info("状态 -> %s %s", state, reason or "")

    def _tooltip(self) -> str:
        pid = self.proc.pid if self.proc and self.proc.poll() is None else "-"
        task = "任务运行中" if self.busy else "空闲"
        text = f"facefind {STATE_CN.get(self.state, self.state)} · {self.url} · PID {pid} · {task}"
        if self.state == STATE_ERROR and self.reason:
            text += f"\n{self.reason}"
        if self.state == STATE_EXTERNAL:
            text += "\n端口上已有实例，本宿主未托管它"
        return text

    def _refresh_tray(self) -> None:
        icon = self.icon
        if icon is None:
            return
        try:
            icon.icon = _icon_image(self.state)
            icon.title = self._tooltip()
            icon.update_menu()
        except Exception:
            _log.debug("托盘刷新失败", exc_info=True)

    def _write_state_file(self) -> None:
        if self.state in (STATE_STOPPED, STATE_EXTERNAL) and not self.proc:
            # 仍然写文件：UI 需要读它判断「是否托管」
            pass
        try:
            DATA.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps({
                "pid": os.getpid(),
                "port": self.port,
                "url": self.url,
                "control_port": self.control_port,
                "token": self.token,
                "state": self.state,
                "child_pid": self.proc.pid if self.proc else 0,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }, ensure_ascii=False), encoding="utf-8")
        except Exception:
            _log.warning("写状态文件失败", exc_info=True)

    # ---------- 子进程 ----------
    def _cmd(self) -> list[str]:
        cmd = [EXE, "-m", "facefind", "--config", self.config, "ui",
               "--port", str(self.port), "--no-browser"]
        cmd.extend(self.extra)
        return cmd

    def _env(self) -> dict:
        env = os.environ.copy()
        env["FACEINFO_ADMIN_TOKEN"] = self.token
        env["FACEINFO_SUPERVISOR_FILE"] = str(self.state_file)
        env["FACEINFO_SUPERVISOR_PID"] = str(os.getpid())
        env["FACEINFO_SUPERVISOR_PORT"] = str(self.control_port)
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def start_service(self) -> bool:
        with self._lock:
            if self.state == STATE_EXTERNAL:
                _log.info("端口 %s 上已有外部实例，跳过启动", self.port)
                return False
            if self.proc and self.proc.poll() is None:
                return True
            if not _port_free(self.port):
                if _facefind_running(self.port):
                    self._set_state(STATE_EXTERNAL)
                    return False
                self._set_state(STATE_ERROR, f"端口 {self.port} 被其它程序占用")
                return False
            self._set_state(STATE_STARTING)
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            log_path = LOG_DIR / "ui_stdout.log"
            log_file = open(log_path, "a", encoding="utf-8")
            try:
                self.proc = subprocess.Popen(
                    self._cmd(), cwd=str(ROOT), env=self._env(),
                    stdout=log_file, stderr=subprocess.STDOUT,
                    creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
                )
            except Exception as exc:
                self._set_state(STATE_ERROR, f"拉起子进程失败：{exc}")
                return False
            finally:
                log_file.close()

        ready = False
        deadline = time.time() + 40
        while time.time() < deadline:
            if self.proc.poll() is not None:
                break
            if _facefind_running(self.port, HOST):
                ready = True
                break
            time.sleep(0.5)
        if not ready:
            rc = self.proc.poll()
            self._set_state(STATE_ERROR,
                            f"启动失败（退出码 {rc}），详见 data/logs/ui_stdout.log")
            return False
        self._set_state(STATE_RUNNING)
        self._write_state_file()
        if self.open_browser:
            threading.Timer(0.3, self.open_ui).start()
        return True

    def _wait_child_exit(self, timeout: float = 12.0) -> None:
        proc = self.proc
        if not proc:
            return
        if proc.poll() is None:
            _post_admin(self.port, "/api/admin/_exit", self.token, {"source": "supervisor"})
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _log.warning("子进程未在 %.0fs 内退出，强制结束", timeout)
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        self.proc = None

    def _wait_port_free(self, timeout: float = 15.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if _port_free(self.port):
                return True
            time.sleep(0.3)
        return _port_free(self.port)

    def stop_service(self) -> bool:
        self._wait_child_exit()
        if not self._wait_port_free():
            self._set_state(STATE_ERROR, f"停止后端口 {self.port} 仍被占用")
            return False
        self._set_state(STATE_STOPPED)
        return True

    def restart_service(self) -> bool:
        _log.info("重启服务")
        if not self.stop_service():
            return False
        return self.start_service()

    def _restart_async(self) -> None:
        """由控制通道触发：先等子进程自行退出，再拉起。"""
        def run() -> None:
            self._wait_child_exit()
            if not self._wait_port_free():
                self._set_state(STATE_ERROR, f"重启失败：端口 {self.port} 未释放")
                self.pending = ""
                return
            ok = self.start_service()
            self.pending = ""
            if not ok and self.state != STATE_EXTERNAL:
                _log.error("重启失败：%s", self.reason)
        threading.Thread(target=run, name="supervisor-restart", daemon=True).start()

    def _shutdown_async(self) -> None:
        def run() -> None:
            self._wait_child_exit()
            self.cleanup()
            _log.info("宿主退出")
            os._exit(0)
        threading.Thread(target=run, name="supervisor-shutdown", daemon=True).start()

    # ---------- 控制通道 ----------
    def _serve_control(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((HOST, 0))
            srv.listen(8)
            srv.settimeout(0.5)
        except OSError as exc:
            _log.error("控制通道启动失败：%s", exc)
            return
        self.control_port = srv.getsockname()[1]
        self.control_server = srv
        self._write_state_file()
        _log.info("控制通道 127.0.0.1:%s", self.control_port)
        while not self.stop_event.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()
        try:
            srv.close()
        except Exception:
            pass

    def _handle_conn(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(3.0)
            raw = conn.recv(4096).decode("utf-8", "replace").strip()
            req = json.loads(raw) if raw else {}
            if req.get("token") != self.token:
                reply = {"ok": False, "error": "bad_token"}
            else:
                reply = self._dispatch(str(req.get("cmd") or ""))
            conn.sendall((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
        except Exception as exc:
            _log.warning("控制通道请求异常：%s", exc)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _dispatch(self, cmd: str) -> dict:
        _log.info("控制命令 %s", cmd)
        if cmd == "ping":
            return {"ok": True, "state": self.state, "pid": os.getpid(),
                    "child_pid": self.proc.pid if self.proc else 0, "port": self.port}
        if cmd == "open":
            self.open_ui(silent=True)
            return {"ok": True, "state": self.state}
        if cmd == "restart":
            self.pending = "restart"
            self._restart_async()
            return {"ok": True, "state": self.state, "reconnect_after": 10}
        if cmd == "shutdown":
            self.pending = "shutdown"
            self._shutdown_async()
            return {"ok": True, "state": self.state}
        if cmd == "stop":
            threading.Thread(target=self.stop_service, daemon=True).start()
            return {"ok": True, "state": "stopping"}
        if cmd == "status":
            return {"ok": True, "state": self.state, "pid": os.getpid(),
                    "child_pid": self.proc.pid if self.proc else 0,
                    "busy": self.busy, "reason": self.reason}
        return {"ok": False, "error": f"unknown_cmd:{cmd}"}

    # ---------- 监控 ----------
    def _monitor(self) -> None:
        while not self.stop_event.is_set():
            time.sleep(2.0)
            try:
                proc = self.proc
                if proc is not None and proc.poll() is not None and self.pending == "":
                    rc = proc.returncode
                    self.proc = None
                    self._set_state(STATE_ERROR, f"子进程异常退出（退出码 {rc}）")
                    continue
                if self.state == STATE_RUNNING:
                    busy = _job_busy(self.port)
                    if busy != self.busy:
                        self.busy = busy
                        self._refresh_tray()
                elif self.state == STATE_EXTERNAL and _port_free(self.port):
                    # 原先复用的外部实例已退出：回到「已停止」，菜单可再次启动
                    self._set_state(STATE_STOPPED, "外部实例已退出")
            except Exception:
                _log.debug("监控循环异常", exc_info=True)

    # ---------- 菜单动作 ----------
    def open_ui(self, silent: bool = False) -> None:
        if self.state == STATE_STOPPED:
            self.start_service()
        try:
            webbrowser.open(self.url)
        except Exception:
            _log.warning("打开浏览器失败", exc_info=True)

    def open_logs(self) -> None:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            os.startfile(str(LOG_DIR))       # noqa: S606 - Windows 本地工具
        except Exception:
            _log.warning("打开日志目录失败", exc_info=True)

    def quit(self) -> None:
        _log.info("托盘菜单：退出")
        self.cleanup()
        try:
            if self.icon:
                self.icon.stop()
        except Exception:
            pass

    def cleanup(self) -> None:
        self.stop_event.set()
        self._wait_child_exit()
        try:
            self.state_file.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            if self.control_server:
                self.control_server.close()
        except Exception:
            pass

    # ---------- 托盘 ----------
    def _menu(self):
        import pystray

        def spawn(fn):
            # 菜单回调直接跑会阻塞托盘消息循环（启动最多 40s），统一丢到后台线程
            return lambda *_: threading.Thread(target=fn, daemon=True).start()

        return pystray.Menu(
            pystray.MenuItem("打开界面", lambda *_: self.open_ui(), default=True),
            pystray.MenuItem("启动服务", spawn(self.start_service),
                             enabled=lambda item: self.state in (STATE_STOPPED, STATE_ERROR)),
            pystray.MenuItem("停止服务", spawn(self.stop_service),
                             enabled=lambda item: self.state == STATE_RUNNING),
            pystray.MenuItem("重启服务", spawn(self.restart_service),
                             enabled=lambda item: self.state == STATE_RUNNING),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("查看日志目录", lambda *_: self.open_logs()),
            pystray.MenuItem("退出", lambda *_: self.quit()),
        )

    def _bootstrap(self) -> None:
        """托盘先出现，再拉起服务，这样「启动中」状态可见。"""
        if _facefind_running(self.port) and not _port_free(self.port):
            self.proc = None
            self._set_state(STATE_EXTERNAL)
        else:
            self.start_service()

    def run(self) -> int:
        mutex = _Mutex(f"Local\\facefind_supervisor_{self.port}")
        if not mutex.acquire():
            print("facefind 托盘已在运行，已复用现有实例。")
            _notify_existing(self.port)
            if self.open_browser:
                webbrowser.open(self.url)
            return 0

        DATA.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        threading.Thread(target=self._serve_control, name="supervisor-control", daemon=True).start()
        # 等控制通道就绪（端口写入状态文件）
        for _ in range(30):
            if self.control_port or self.stop_event.is_set():
                break
            time.sleep(0.1)

        try:
            import pystray
        except ImportError:
            print("未安装 pystray：请执行 python -m pip install pystray pillow")
            self._bootstrap()
            print(f"服务已启动：{self.url}  （Ctrl+C 结束）")
            try:
                while not self.stop_event.is_set():
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
            self.cleanup()
            mutex.release()
            return 0

        self.icon = pystray.Icon(
            "facefind", _icon_image(self.state), self._tooltip(), menu=self._menu()
        )
        threading.Thread(target=self._bootstrap, name="supervisor-bootstrap", daemon=True).start()
        threading.Thread(target=self._monitor, name="supervisor-monitor", daemon=True).start()
        try:
            self.icon.run()
        finally:
            self.cleanup()
            mutex.release()
        return 0


def _notify_existing(port: int) -> None:
    """向同一端口的已有宿主发送 open 命令（找不到就忽略）。"""
    path = DATA / f"supervisor_{int(port)}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        control_port = int(data.get("control_port") or 0)
        token = str(data.get("token") or "")
        if not control_port or not token:
            return
        with socket.create_connection((HOST, control_port), timeout=2) as sock:
            sock.sendall((json.dumps({"cmd": "open", "token": token}) + "\n").encode())
            sock.recv(4096)
    except Exception:
        return


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"supervisor_{datetime.now():%Y%m%d}.log"
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger("facefind.supervisor")
    root.setLevel(logging.INFO)
    if not root.handlers:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="facefind.supervisor", description="facefind UI 托盘宿主（管理启动/重启/退出）"
    )
    parser.add_argument("--port", type=int, default=8765, help="端口（默认 8765）")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--no-tray", action="store_true",
                        help="不使用托盘宿主，退回原来的无宿主模式（等价于 facefind ui）")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args, extra = parser.parse_known_args(argv)

    if args.no_tray:
        from .cli import main as cli_main
        child = ["--config", args.config, "ui", "--port", str(args.port)]
        if args.no_browser:
            child.append("--no-browser")
        return cli_main(child)

    _setup_logging()
    _log.info("启动托盘宿主 port=%s config=%s extra=%s", args.port, args.config, extra)
    supervisor = Supervisor(args.port, args.config, extra, open_browser=not args.no_browser)
    try:
        return supervisor.run()
    except KeyboardInterrupt:
        supervisor.cleanup()
        return 0
    except Exception:
        _log.exception("宿主异常退出")
        supervisor.cleanup()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
