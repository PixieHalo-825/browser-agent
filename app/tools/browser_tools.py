"""Browser tools. Click/type go through the GUI-Plus dual-channel locator."""

from __future__ import annotations

import logging
import time
from typing import Any

from app.browser.actions import click, type_text
from app.browser.browser import get_browser
from app.browser.dom_extractor import extract_interactive_elements, format_elements
from app.browser.locator import get_locator
from app.tools.base import BaseTool

logger = logging.getLogger(__name__)


class BrowserNavigate(BaseTool):
    name = "browser_navigate"
    description = (
        "打开指定 URL 的网页。支持 http/https/file 协议。"
        "返回页面标题和可交互元素概览。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "要访问的完整 URL"},
        },
        "required": ["url"],
    }

    async def execute(self, url: str, **kwargs: Any) -> str:
        page = await get_browser().start()
        try:
            await page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            return f"导航失败: {exc}"
        title = await page.title()
        elements = await extract_interactive_elements(page)
        return f"已打开: {url}\n标题: {title}\n当前可交互元素:\n{format_elements(elements)}"


class BrowserClick(BaseTool):
    name = "browser_click"
    description = (
        "点击网页上的元素。target 用自然语言描述要点击的元素"
        "（如 '登录按钮'、'搜索框旁边的放大镜图标'、'价格排序下拉框'）。"
        "系统会先解析 DOM 文本定位，失败时自动用视觉模型兜底。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "目标元素的自然语言描述"},
        },
        "required": ["target"],
    }

    async def execute(self, target: str, **kwargs: Any) -> str:
        page = await get_browser().start()
        result = await get_locator().locate(page, target)
        logger.info("[GUI-Plus] %s", result.describe())
        if not result.success:
            return f"无法定位目标元素 '{target}': {result.reason}"
        outcome = await click(page, result)
        await page.wait_for_timeout(600)  # let handlers/animations settle
        title = await page.title()
        return f"{outcome}\n点击后页面标题: {title}"


class BrowserType(BaseTool):
    name = "browser_type"
    description = (
        "在输入框中输入文字。target 用自然语言描述输入框"
        "（如 '搜索输入框'、'用户名输入框'）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "目标输入框的自然语言描述"},
            "text": {"type": "string", "description": "要输入的内容"},
            "press_enter": {"type": "boolean", "description": "输入后是否按回车提交"},
        },
        "required": ["target", "text"],
    }

    async def execute(self, target: str, text: str, press_enter: bool = False, **kwargs: Any) -> str:
        page = await get_browser().start()
        result = await get_locator().locate(page, target)
        logger.info("[GUI-Plus] %s", result.describe())
        if not result.success:
            return f"无法定位输入框 '{target}': {result.reason}"
        outcome = await type_text(page, result, text)
        if press_enter:
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(800)
            outcome += " 并按下回车"
        return outcome


class BrowserScroll(BaseTool):
    name = "browser_scroll"
    description = "向上或向下滚动页面。direction 为 up 或 down。"
    parameters = {
        "type": "object",
        "properties": {
            "direction": {"type": "string", "enum": ["up", "down"]},
            "amount": {"type": "number", "description": "滚动像素数，默认 500"},
        },
        "required": ["direction"],
    }

    async def execute(self, direction: str = "down", amount: float = 500, **kwargs: Any) -> str:
        page = await get_browser().start()
        delta = -abs(amount) if direction == "up" else abs(amount)
        await page.mouse.wheel(0, delta)
        await page.wait_for_timeout(400)
        elements = await extract_interactive_elements(page)
        return f"已向{direction}滚动 {abs(amount)}px\n当前可见可交互元素:\n{format_elements(elements)}"


class BrowserScreenshot(BaseTool):
    name = "browser_screenshot"
    description = "截图保存当前页面，用于记录证据或供后续视觉分析。"
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "截图文件名（不含扩展名）"},
        },
        "required": [],
    }

    async def execute(self, name: str = "", **kwargs: Any) -> str:
        from app.config import get_config

        ws = get_config().workspace_root()
        filename = f"{name or 'screenshot'}-{int(time.time())}.png"
        path = ws / filename
        page = await get_browser().start()
        await page.screenshot(path=str(path))
        return f"截图已保存: {path}"


class BrowserExtract(BaseTool):
    name = "browser_extract"
    description = (
        "提取当前页面的正文文本内容（去除导航等噪声），用于读取商品信息、"
        "价格、文章内容等。可传关键词过滤包含该关键词的行。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "keyword": {"type": "string", "description": "可选，只保留包含此关键词的文本行"},
        },
        "required": [],
    }

    async def execute(self, keyword: str = "", **kwargs: Any) -> str:
        page = await get_browser().start()
        text = await page.evaluate(
            "() => document.body ? document.body.innerText : ''"
        )
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if keyword:
            lines = [ln for ln in lines if keyword in ln]
        content = "\n".join(lines[:200])
        if not content:
            return "页面无可提取文本" + (f"（关键词 {keyword!r} 无匹配）" if keyword else "")
        return content[:6000]
