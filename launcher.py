"""Browser-Agent 图形启动器。

轻量 Tkinter 窗口：环境自检（venv / Playwright / API Key / Docker）+
一键启动 Web 界面、CLI 控制台、定位评测、测试套件。

双击 launcher.bat 或运行:  .venv\\Scripts\\python launcher.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from tkinter import BOTH, BOTTOM, LEFT, RIGHT, X, ttk
import tkinter as tk

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
    """MIMO_API_KEY / LLM_API_KEY env or api_key in config.toml."""
    if os.environ.get("MIMO_API_KEY") or os.environ.get("LLM_API_KEY"):
        return True
    try:
        import tomllib

        for name in ("config.toml", "config.example.toml"):
            path = ROOT / name
            if path.exists():
                with open(path, "rb") as f:
                    data = tomllib.load(f)
                if data.get("llm", {}).get("api_key"):
                    return True
    except Exception:
        pass
    return False


def detect_playwright() -> bool:
    try:
        import playwright  # noqa: F401

        return True
    except ImportError:
        return False


def detect_docker() -> bool:
    try:
        subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, timeout=4,
        )
        return True
    except Exception:
        return False


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
        self.btn_web = self._action_button(box, "🌐  Web 界面  (推荐)", ACCENT, self.toggle_web, first=True)
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
            set_result("api_key", has_key, "已配置" if has_key else "MIMO_API_KEY 未设置")
            has_docker = detect_docker()
            set_result("docker", has_docker, "完整隔离" if has_docker else "未安装，沙箱降级 local")
            self.after(0, lambda: self.status.config(
                text="环境就绪" if (has_key and VENV_PY.exists()) else "环境不完整，见上方检查项"
            ))

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
