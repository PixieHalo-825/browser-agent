"""Browser-Agent CLI entry point.

Interactive loop: type a task, watch the Planner/Executor/Reviewer state
machine work, get the final answer. ``exit``/``quit`` to leave.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from app.agent.graph import run_agent
from app.config import get_config

console = Console()

BANNER = r"""
[cyan] ____             _                          _     ____ _     ___                    [/cyan]
[cyan]| __ )  __ _  ___| | ____ _ _ __ ___  _   _ / \   / ___| |   |_ _|                    [/cyan]
[cyan]|  _ \ / _` |/ __| |/ / _` | '__/ _ \| | | / _ \ | |   | |    | |                     [/cyan]
[cyan]| |_) | (_| | (__|   <  __/ | | | (_) | |_|/ ___ \| |___| |___ | |                     [/cyan]
[cyan]|____/ \__,_|\___|_|\_\___|_|  \___/ \__, /_/   \_\____|_____|___|                     [/cyan]
[cyan]  通用浏览器自动化智能体 · OpenManus 架构 + LangGraph 分层状态机            |___/       [/cyan]
"""

NODE_ICONS = {
    "planner": ("📋 Planner", "cyan"),
    "executor": ("⚡ Executor", "yellow"),
    "tools": ("🔧 Tools", "green"),
    "reviewer": ("🔍 Reviewer", "magenta"),
    "finish": ("🏁 Finish", "blue"),
}


def print_env_report() -> None:
    cfg = get_config()
    table = Table(title="环境检查", show_header=False, border_style="dim")
    table.add_column(style="bold")
    table.add_column()
    table.add_row("LLM 端点", cfg.llm.base_url)
    table.add_row("文本模型", cfg.llm.model)
    table.add_row("视觉模型", cfg.llm.vision_model)
    ok_key = "✅ 已配置" if cfg.llm.api_key else "❌ 未配置（config.toml 或环境变量 LLM_API_KEY）"
    table.add_row("API Key", ok_key)
    table.add_row("视觉兜底", "✅ 开启" if cfg.vision.enabled else "⛔ 关闭")
    table.add_row("沙箱策略", cfg.sandbox.provider)
    table.add_row("MCP servers", ", ".join(cfg.mcp.servers) if cfg.mcp.enabled else "(disabled)")
    console.print(table)
    if not cfg.llm.api_key:
        console.print("[bold red]缺少 LLM API Key，无法执行任务。[/bold red] 请复制 config.example.toml 为 config.toml 并填写。")
        sys.exit(1)


async def on_event(node: str, update: dict) -> None:
    """Live rendering of state-machine progress."""
    label, color = NODE_ICONS.get(node, (node, "white"))

    if node == "planner" and "plan" in update:
        plan = update["plan"]
        lines = [f"  {i+1}. {s['description']}" for i, s in enumerate(plan)]
        console.print(Panel("\n".join(lines), title=f"[{color}]{label} 生成的计划 ({len(plan)} 步)[/{color}]", border_style=color))

    elif node == "executor" and "messages" in update:
        for message in update["messages"]:
            if message.get("role") == "assistant" and message.get("tool_calls"):
                for call in message["tool_calls"]:
                    console.print(f"[{color}]{label}[/{color}] 调用 → [bold]{call['function']['name']}[/bold]({call['function']['arguments'][:100]})")
            elif message.get("role") == "user":
                console.print(f"[dim]{message['content'][:120]}[/dim]")

    elif node == "tools" and "messages" in update:
        for message in update["messages"]:
            content = (message.get("content") or "").replace("\n", " ")
            console.print(f"[{color}]{label}[/{color}] 结果 ← [dim]{content[:160]}[/dim]")

    elif node == "reviewer":
        verdict = update.get("verdict", "")
        reason = update.get("verdict_reason", "")
        if verdict:
            style = {"continue": "green", "replan": "yellow", "finish": "cyan"}.get(verdict, "white")
            console.print(f"[{color}]{label}[/{color}] 判定 → [bold {style}]{verdict}[/bold {style}] [dim]{reason}[/dim]")

    elif node == "finish":
        console.print(f"[blue]🏁 Finish[blue]")

    if update.get("final_answer") and node in ("reviewer", "finish"):
        console.print(Panel(update["final_answer"], title="[bold blue]最终答案[/bold blue]", border_style="blue"))


def _list_models() -> None:
    from app.llm.llm import active_preset_id, build_config_from_preset, get_llm

    presets = get_config().model_presets
    current = active_preset_id() or "default"
    console.print(f"[bold]当前模型:[/bold] {get_llm().config.model}（预设: {current}）")
    table = Table(title="可切换模型预设（输入: model <id>）", show_header=True, border_style="dim")
    table.add_column("id", style="cyan")
    table.add_column("label")
    table.add_column("model")
    table.add_column("端点", style="dim")
    table.add_column("Key", justify="center")
    for preset in presets:
        merged = build_config_from_preset(preset)
        marker = " ←" if preset.id == current else ""
        table.add_row(
            preset.id + marker,
            preset.label,
            merged.model,
            merged.base_url.split("//")[-1].split("/")[0],
            "✅" if merged.api_key else "❌",
        )
    console.print(table)


def _switch_model(preset_id: str) -> None:
    from app.llm.llm import apply_model_preset, reset_llm

    if preset_id in ("default", "默认"):
        cfg = reset_llm(None)
        console.print(f"[green]已切回默认模型:[/green] {cfg.model}")
        return
    preset = next((p for p in get_config().model_presets if p.id == preset_id), None)
    if preset is None:
        console.print(f"[red]未知预设: {preset_id}[/red]（输入 model 查看列表）")
        return
    if not preset.api_key and not preset.api_key_env:
        # inherit key from [llm]; nothing to check
        pass
    elif not preset.api_key and preset.api_key_env and not __import__("os").environ.get(preset.api_key_env):
        console.print(f"[yellow]警告:[/yellow] 预设 {preset.id} 依赖环境变量 {preset.api_key_env}，当前未设置")
    cfg = apply_model_preset(preset)
    console.print(f"[green]已切换到[/green] {preset.label}（model={cfg.model}, vision={cfg.vision_model}）")


async def main() -> None:
    console.print(BANNER)
    print_env_report()

    from app.sandbox.manager import create_sandbox

    try:
        sandbox = await create_sandbox()
        console.print(f"[green]沙箱就绪:[/green] {sandbox.name} (provider={get_config().sandbox.provider})")
    except Exception as exc:
        console.print(f"[yellow]沙箱初始化警告:[/yellow] {exc}")

    console.print("\n输入任务开始，例如：\n"
                  "[dim]  • 打开 https://example.com 并提取页面标题\n"
                  "  • 搜索\"Playwright 最新版本\"，总结要点并保存到 notes.md\n"
                  "  • model          查看可切换的模型预设\n"
                  "  • model <id>     切换模型（如 model mimo-flash）\n"
                  "  • exit 退出[/dim]\n")

    while True:
        try:
            task = Prompt.ask("[bold green]任务[/bold green]").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not task:
            continue
        if task.lower() in ("exit", "quit", "q", "退出"):
            break

        if task.lower() == "model":
            _list_models()
            continue
        if task.lower().startswith("model "):
            _switch_model(task.split(None, 1)[1].strip())
            continue

        console.rule(f"[bold]执行任务: {task}[/bold]")
        try:
            result = await run_agent(task, on_event=on_event)
        except KeyboardInterrupt:
            console.print("[yellow]任务已中断[/yellow]")
            continue
        except Exception as exc:
            console.print(f"[bold red]任务失败:[/bold red] {exc}")
            logging.getLogger(__name__).exception("agent run crashed")
            continue

        answer = result.get("final_answer") or "(无最终答案)"
        console.print(Panel(answer, title="[bold blue]✅ 任务完成[/bold blue]", border_style="blue"))
        console.print()

    console.print("[dim]Bye 👋[/dim]")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
