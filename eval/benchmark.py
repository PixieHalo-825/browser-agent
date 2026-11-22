"""Element-location benchmark harness.

Runs a fixed set of locate-and-click tasks against local HTML scenarios in two
modes and verifies the actual click effect (``window.__clicked``) — success is
NOT "the locator returned something", it is "the right thing happened on the
page". This is what makes the reported numbers reproducible.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.browser.actions import click
from app.browser.browser import get_browser
from app.browser.locator import get_locator

logger = logging.getLogger(__name__)

CASES_DIR = Path(__file__).resolve().parent / "cases"


@dataclass
class LocateTask:
    scenario: str                     # html file stem in cases/
    name: str                         # human-readable task name
    target: str                       # natural-language element description
    expected: str                     # expected window.__clicked value
    setup_click: Optional[str] = None # data-test selector clicked before locating
    category: str = "general"         # icon | canvas | popup | duplicate | baseline


@dataclass
class TaskResult:
    task: LocateTask
    success: bool
    channel: str = "-"          # dom-rule | dom-llm | vision-som | vision-ground | failed
    reason: str = ""
    latency_s: float = 0.0
    clicked_value: str = ""


TASKS: list[LocateTask] = [
    # -- icon buttons: no text/aria/name/id in DOM -------------------------
    LocateTask("icon_toolbar", "购物车图标", "购物车图标按钮", "cart", category="icon"),
    LocateTask("icon_toolbar", "放大镜搜索图标", "放大镜（搜索）图标按钮", "search", category="icon"),
    LocateTask("icon_toolbar", "红色心形收藏图标", "红色的心形（收藏）图标按钮", "favorite", category="icon"),
    LocateTask("icon_toolbar", "下载图标", "下载图标按钮", "download", category="icon"),
    LocateTask("icon_toolbar", "打印机图标", "打印机图标按钮", "print", category="icon"),
    # -- canvas: no DOM elements at all -------------------------------------
    LocateTask("canvas_shapes", "红色圆形按钮", "红色的圆形按钮", "circle", category="canvas"),
    LocateTask("canvas_shapes", "蓝色正方形按钮", "蓝色的正方形按钮", "square", category="canvas"),
    LocateTask("canvas_shapes", "绿色三角形按钮", "绿色的三角形按钮", "triangle", category="canvas"),
    LocateTask("canvas_shapes", "黄色五角星按钮", "黄色的五角星按钮", "star", category="canvas"),
    # -- popup overlay: SVG close button without text -----------------------
    LocateTask("popup_overlay", "基线-文本按钮", "领取优惠券按钮", "claim", category="baseline"),
    LocateTask("popup_overlay", "弹窗叉号关闭按钮", "弹窗右上角的关闭（叉号）按钮", "close",
               setup_click="claim", category="popup"),
    # -- duplicate labels: same text, different cards -----------------------
    LocateTask("duplicate_labels", "指定商品的查看价格", "无线耳机 Pro 商品的查看价格按钮", "itemB",
               category="duplicate"),
]


def case_url(scenario: str) -> str:
    return (CASES_DIR / f"{scenario}.html").resolve().as_uri()


async def run_task(task: LocateTask, mode: str) -> TaskResult:
    """Run a single locate-and-click task in the given mode."""
    browser = get_browser()
    locator = get_locator()
    page = await browser.new_page()
    started = time.perf_counter()
    try:
        await page.goto(case_url(task.scenario), wait_until="load")
        await page.wait_for_timeout(250)
        if task.setup_click:
            await page.click(f'[data-test="{task.setup_click}"]')
            await page.wait_for_timeout(350)

        result = await locator.locate(page, task.target, mode=mode)
        if not result.success:
            return TaskResult(
                task, False, channel="failed", reason=result.reason,
                latency_s=round(time.perf_counter() - started, 2),
            )

        await click(page, result)
        await page.wait_for_timeout(300)
        clicked = await page.evaluate("window.__clicked || ''")
        return TaskResult(
            task,
            success=(clicked == task.expected),
            channel=result.channel,
            reason="" if clicked == task.expected else f"clicked={clicked!r}, want {task.expected!r}",
            latency_s=round(time.perf_counter() - started, 2),
            clicked_value=clicked,
        )
    except Exception as exc:
        return TaskResult(task, False, channel="error", reason=str(exc)[:200],
                          latency_s=round(time.perf_counter() - started, 2))
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def run_benchmark(mode: str) -> list[TaskResult]:
    """Run every task once in the given mode."""
    logger.info("Running benchmark in mode=%s (%d tasks)", mode, len(TASKS))
    results = []
    for task in TASKS:
        result = await run_task(task, mode)
        mark = "PASS" if result.success else "FAIL"
        logger.info("  [%s] %-22s -> %-14s %s (%.1fs)",
                    mark, task.name, result.channel, result.reason, result.latency_s)
        results.append(result)
    return results


def summarize(results: list[TaskResult]) -> dict:
    total = len(results)
    success = sum(1 for r in results if r.success)
    by_channel: dict[str, int] = {}
    for r in results:
        by_channel[r.channel] = by_channel.get(r.channel, 0) + 1
    by_category: dict[str, dict] = {}
    for r in results:
        bucket = by_category.setdefault(r.task.category, {"total": 0, "success": 0})
        bucket["total"] += 1
        bucket["success"] += int(r.success)
    return {
        "total": total,
        "success": success,
        "success_rate": round(100.0 * success / total, 1) if total else 0.0,
        "avg_latency": round(sum(r.latency_s for r in results) / total, 2) if total else 0.0,
        "by_channel": by_channel,
        "by_category": by_category,
    }
