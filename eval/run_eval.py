"""Element-location success-rate comparison: DOM-only vs GUI-Plus.

Usage:
    python eval/run_eval.py                 # run both modes, print report
    python eval/run_eval.py --mode dom-only
    python eval/run_eval.py --mode gui-plus
    python eval/run_eval.py --json out.json # also dump raw results

Requires an LLM API key (config.toml / LLM_API_KEY) because the dom-llm and
vision channels call the model.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

# Make the project root importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.table import Table

from app.browser.browser import get_browser
from app.config import get_config
from eval.benchmark import TASKS, run_benchmark, summarize

console = Console()


def render_report(dom_results, gui_results) -> str:
    """Build the markdown comparison report."""
    lines = ["# 元素定位成功率评测报告（DOM-only vs GUI-Plus）", ""]
    lines.append(f"- 时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- 任务数: {len(TASKS)}（本地场景，可完全复现）")
    lines.append(f"- 文本模型: {get_config().llm.model} / 视觉模型: {get_config().llm.vision_model}")
    lines.append("")

    header = "| 场景 | 任务 | DOM-only | GUI-Plus |"
    sep = "|---|---|---|---|"
    lines.append(header)
    lines.append(sep)

    dom_by_name = {r.task.name: r for r in (dom_results or [])}
    gui_by_name = {r.task.name: r for r in (gui_results or [])}
    for task in TASKS:
        dom = dom_by_name.get(task.name)
        gui = gui_by_name.get(task.name)
        dom_cell = _cell(dom)
        gui_cell = _cell(gui)
        lines.append(f"| {task.scenario} | {task.name} | {dom_cell} | {gui_cell} |")

    lines.append("")
    if dom_results:
        s = summarize(dom_results)
        lines.append(f"**DOM-only 成功率: {s['success']}/{s['total']} = {s['success_rate']}%**"
                     f"（平均耗时 {s['avg_latency']}s）")
    if gui_results:
        s = summarize(gui_results)
        lines.append(f"**GUI-Plus 成功率: {s['success']}/{s['total']} = {s['success_rate']}%**"
                     f"（平均耗时 {s['avg_latency']}s，通道分布: {s['by_channel']}）")
    if dom_results and gui_results:
        d = summarize(dom_results)["success_rate"]
        g = summarize(gui_results)["success_rate"]
        lines.append("")
        lines.append(f"## 结论: 元素定位成功率 {d}% → {g}%"
                     f"（提升 {round(g - d, 1)} 个百分点）")
    return "\n".join(lines)


def _cell(result) -> str:
    if result is None:
        return "-"
    if result.success:
        return f"✅ {result.channel}"
    return f"❌ {result.channel}" + (f" ({result.reason})" if result.reason else "")


def print_table(dom_results, gui_results) -> None:
    table = Table(title="元素定位成功率对比", show_lines=False)
    table.add_column("任务", style="bold")
    table.add_column("类别")
    if dom_results is not None:
        table.add_column("DOM-only")
    if gui_results is not None:
        table.add_column("GUI-Plus")
    dom_by_name = {r.task.name: r for r in (dom_results or [])}
    gui_by_name = {r.task.name: r for r in (gui_results or [])}
    for task in TASKS:
        row = [task.name, task.category]
        if dom_results is not None:
            r = dom_by_name[task.name]
            row.append(("✅ " if r.success else "❌ ") + r.channel)
        if gui_results is not None:
            r = gui_by_name[task.name]
            row.append(("✅ " if r.success else "❌ ") + r.channel)
        table.add_row(*row)
    console.print(table)

    if dom_results:
        s = summarize(dom_results)
        console.print(f"DOM-only : [bold]{s['success']}/{s['total']} = {s['success_rate']}%[/bold]  通道: {s['by_channel']}")
    if gui_results:
        s = summarize(gui_results)
        console.print(f"GUI-Plus: [bold green]{s['success']}/{s['total']} = {s['success_rate']}%[/bold green]  通道: {s['by_channel']}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="定位成功率对比评测")
    parser.add_argument("--mode", choices=["both", "dom-only", "gui-plus"], default="both")
    parser.add_argument("--json", dest="json_out", default="", help="结果 JSON 输出路径")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")

    cfg = get_config()
    if not cfg.llm.api_key:
        console.print("[bold red]未配置 LLM API Key（dom-llm / vision 通道不可用）。"
                      "请先配置 config.toml。[/bold red]")
        return 1

    browser = get_browser()
    dom_results = None
    gui_results = None
    try:
        await browser.start()
        if args.mode in ("both", "dom-only"):
            console.rule("[cyan]模式 1/2: DOM-only（关闭视觉兜底）[/cyan]")
            dom_results = await run_benchmark("dom-only")
        if args.mode in ("both", "gui-plus"):
            console.rule("[cyan]模式 2/2: GUI-Plus（DOM + Vision 双通道）[/cyan]")
            gui_results = await run_benchmark("gui-plus")
    finally:
        await browser.stop()

    print_table(dom_results, gui_results)
    report = render_report(dom_results, gui_results)
    console.print()
    console.print(report)

    report_path = get_config().workspace_root() / f"eval-report-{time.strftime('%Y%m%d-%H%M%S')}.md"
    report_path.write_text(report, encoding="utf-8")
    console.print(f"\n[dim]报告已保存: {report_path}[/dim]")

    if args.json_out:
        payload = {
            "dom_only": [r.__dict__ | {"task": r.task.__dict__} for r in (dom_results or [])],
            "gui_plus": [r.__dict__ | {"task": r.task.__dict__} for r in (gui_results or [])],
        }
        Path(args.json_out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        console.print(f"[dim]JSON 已保存: {args.json_out}[/dim]")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
