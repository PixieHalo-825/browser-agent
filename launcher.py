"""Browser-Agent 图形启动器。

轻量 Tkinter 窗口：环境自检（venv / Playwright / API Key / Docker）+
一键启动 Web 界面、CLI 控制台、定位评测、测试套件。
Docker Desktop 已安装但未运行时，可直接在这里把它启动起来。

双击 launcher.bat 或运行:  .venv\\Scripts\\python launcher.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from tkinter import BOTH, BOTTOM, LEFT, RIGHT, X, ttk
import tkinter as tk

from app.config import api_key_env_for, get_config
from app.sandbox.docker_sandbox import docker_engine_running, find_docker_desktop, launch_docker_desktop

ROOT = Path(__file__).resolve().parent
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
PY = str(VENV_PY if VENV_PY.exists() else sys.executable)

BG = "#0d1117"
PANEL = "#161b22"
FG = "#e6edf3"
DIM = "#8b949e"
ACCENT = "#58a6ff"
GREEN = "#3fb950"
YELLOW = "#d29922"
RED = "#f85149"
BORDER = "#30363d"


def detect_api_key() -> bool:
    """True when the active config resolves to a non-empty API key.

    Goes through the real config loader so the check follows whatever endpoint
    (and therefore whichever key variable) the config points at.
    """
    try:
        return bool(get_config().llm.api_key)
    except Exception:
        return False


def detect_playwright() -> bool:
    try:
        import playwright  # noqa: F401

        return True
    except ImportError:
        return False


def detect_docker() -> tuple[bool, bool]:
    """(Docker Desktop is installed, its engine is answering).

    Goes through the Docker SDK, so it needs neither the docker CLI on PATH
    nor a running container to tell "stopped" from "not installed".
    """
    installed = find_docker_desktop() is not None
    return installed, docker_engine_running()


class Launcher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Browser-Agent 启动器")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.web_proc: subprocess.Popen | None = None

        self._build_header()
        self._build_checks()
        self._build_buttons()
        self._build_statusbar()

        self._start_checks()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # --- UI ------------------------------------------------------------------

    def _build_header(self):
        frame = tk.Frame(self, bg=BG)
        frame.pack(fill=X, padx=24, pady=(20, 8))
        tk.Label(
            frame, text="⌨ Browser-Agent", bg=BG, fg=FG,
            font=("Microsoft YaHei UI", 16, "bold"),
        ).pack(side=LEFT)
        tk.Label(
            frame, text="  v0.1", bg=BG, fg=DIM,
            font=("Microsoft YaHei UI", 10),
        ).pack(side=LEFT, pady=(6, 0))

    def _build_checks(self):
        box = tk.Frame(self, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        box.pack(fill=X, padx=24, pady=(8, 12))
        tk.Label(
            box, text="环境检查", bg=PANEL, fg=DIM,
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(anchor="w", padx=12, pady=(8, 2))
        self.check_vars: dict[str, tk.Label] = {}
        for key, label in (
            ("venv", "虚拟环境 .venv"),
            ("playwright", "Playwright 依赖"),
            ("api_key", "模型 API Key"),
            ("docker", "Docker 沙箱"),
        ):
            row = tk.Frame(box, bg=PANEL)
            row.pack(fill=X, padx=12, pady=2)
            dot = tk.Label(row, text="…", bg=PANEL, fg=DIM, width=2, anchor="w",
                           font=("Microsoft YaHei UI", 10))
            dot.pack(side=LEFT)
            tk.Label(row, text=label, bg=PANEL, fg=FG, anchor="w",
                     font=("Microsoft YaHei UI", 10)).pack(side=LEFT)
            note = tk.Label(row, text="", bg=PANEL, fg=DIM, anchor="e",
                            font=("Microsoft YaHei UI", 9))
            note.pack(side=RIGHT)
            self.check_vars[key] = (dot, note)

    def _build_buttons(self):
        box = tk.Frame(self, bg=BG)
        box.pack(fill=BOTH, expand=True, padx=24)
        # Only shown when Docker Desktop is installed but its engine is stopped.
        self.btn_docker = self._action_button(box, "🐳  启动 Docker Desktop", YELLOW, self.start_docker, first=True)
        self.btn_docker.pack_forget()
        self.btn_web = self._action_button(box, "🌐  Web 界面  (推荐)", ACCENT, self.toggle_web)
        self._action_button(box, "⌨  CLI 控制台", FG, self.run_cli)
        self._action_button(box, "📊  定位成功率评测", FG, self.run_eval)
        self._action_button(box, "✅  测试套件 (pytest)", FG, self.run_tests)

    def _action_button(self, parent, text, color, command, first=False):
        btn = tk.Button(
            parent, text=text, command=command,
            bg=PANEL, fg=color, activebackground="#21262d", activeforeground=color,
            relief="flat", bd=0, highlightthickness=1, highlightbackground=BORDER,
            font=("Microsoft YaHei UI", 11, "bold"), cursor="hand2",
            pady=10,
        )
        btn.pack(fill=X, pady=(0 if first else 8, 8))
        btn.bind("<Enter>", lambda e, b=btn, c=color: b.config(fg=c, highlightthickness=2))
        btn.bind("<Leave>", lambda e, b=btn, c=color: b.config(fg=c, highlightthickness=1))
        return btn

    def _build_statusbar(self):
        self.status = tk.Label(
            self, text="就绪", bg=PANEL, fg=DIM, anchor="w",
            font=("Microsoft YaHei UI", 9), padx=12, pady=6,
        )
        self.status.pack(fill=X, side=BOTTOM)

    # --- checks ---------------------------------------------------------------

    def _start_checks(self):
        def set_result(key: str, ok: bool, note: str):
            def apply():
                dot, note_lbl = self.check_vars[key]
                dot.config(text="●", fg=GREEN if ok else (YELLOW if note else RED))
                note_lbl.config(text=note)
            self.after(0, apply)

        def worker():
            set_result("venv", VENV_PY.exists(), "就绪" if VENV_PY.exists() else "未创建，先 python -m venv .venv")
            set_result("playwright", detect_playwright(), "已安装" if detect_playwright() else "未安装")
            has_key = detect_api_key()
            key_env = api_key_env_for(get_config().llm.base_url) or "LLM_API_KEY"
            set_result("api_key", has_key, "已配置" if has_key else f"{key_env} 未设置")
            installed, running = detect_docker()
            if running:
                docker_note = "完整隔离"
            elif installed:
                docker_note = "未运行，可点击下方按钮启动"
            else:
                docker_note = "未安装，沙箱降级 local"
            set_result("docker", running, docker_note)
            self.after(0, lambda: self._show_docker_button(installed and not running))
            self.after(0, lambda: self.status.config(
                text="环境就绪" if (has_key and VENV_PY.exists()) else "环境不完整，见上方检查项"
            ))

        threading.Thread(target=worker, daemon=True).start()

    # --- Docker ----------------------------------------------------------------

    def _show_docker_button(self, show: bool):
        if show:
            self.btn_docker.pack(fill=X, pady=(0, 8), before=self.btn_web)
        else:
            self.btn_docker.pack_forget()

    def start_docker(self):
        """Start Docker Desktop and wait for its engine, off the UI thread."""
        self.btn_docker.config(state="disabled", text="🐳  正在启动 Docker Desktop…")
        self.status.config(text="正在启动 Docker Desktop，首次启动约需 20–60 秒…")

        def worker():
            launched = launch_docker_desktop()
            deadline = time.monotonic() + 120
            running = False
            while launched and time.monotonic() < deadline:
                if docker_engine_running():
                    running = True
                    break
                time.sleep(2)

            def done():
                self.btn_docker.config(state="normal", text="🐳  启动 Docker Desktop")
                self._show_docker_button(not running)
                dot, note = self.check_vars["docker"]
                dot.config(fg=GREEN if running else YELLOW)
                note.config(text="完整隔离" if running else "未运行，可点击下方按钮启动")
                self.status.config(
                    text="Docker 引擎已启动，沙箱完整隔离可用" if running
                    else "Docker Desktop 未能启动，沙箱将降级为本地模式"
                )

            self.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    # --- actions --------------------------------------------------------------

    def _spawn_console(self, args: list[str], title: str) -> subprocess.Popen:
        """Run a command in a new console window (Windows)."""
        creationflags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
        return subprocess.Popen(
            [PY, *args], cwd=str(ROOT), creationflags=creationflags,
            env={**os.environ, "BA_CONSOLE_TITLE": title},
        )

    def toggle_web(self):
        if self.web_proc and self.web_proc.poll() is None:
            self.web_proc.terminate()
            self.web_proc = None
            self.btn_web.config(text="🌐  Web 界面  (推荐)")
            self.status.config(text="Web 服务已停止")
            return
        self.status.config(text="Web 服务启动中… http://127.0.0.1:8686")
        creationflags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
        self.web_proc = subprocess.Popen(
            [PY, "webui.py"], cwd=str(ROOT), creationflags=creationflags,
        )
        self.btn_web.config(text="⏹  停止 Web 服务")
        self.after(2500, lambda: webbrowser.open("http://127.0.0.1:8686"))
        self.status.config(text="Web 服务运行中 → http://127.0.0.1:8686")

    def run_cli(self):
        self._spawn_console(["main.py"], "CLI")
        self.status.config(text="已在新窗口启动 CLI 控制台")

    def run_eval(self):
        self._spawn_console(["eval/run_eval.py"], "eval")
        self.status.config(text="已在新窗口启动定位评测（需 API Key）")

    def run_tests(self):
        self._spawn_console(["-m", "pytest", "tests/", "--asyncio-mode=auto", "-v"], "pytest")
        self.status.config(text="已在新窗口运行测试套件")

    def _on_close(self):
        if self.web_proc and self.web_proc.poll() is None:
            self.web_proc.terminate()
        self.destroy()


if __name__ == "__main__":
    Launcher().mainloop()
