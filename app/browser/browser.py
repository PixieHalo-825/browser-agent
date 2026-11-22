"""Playwright browser lifecycle management.

A singleton async Chromium context with light anti-bot hardening (custom UA,
locale, disabled automation flags, patched ``navigator.webdriver``).
"""

from __future__ import annotations

import logging
from typing import Optional

from playwright.async_api import Browser as PwBrowser
from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from app.config import get_config

logger = logging.getLogger(__name__)


class Browser:
    """Lazily-started Chromium instance shared across the agent run."""

    def __init__(self):
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[PwBrowser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    async def start(self) -> Page:
        if self._page:
            return self._page
        cfg = get_config().browser
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=cfg.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        self._context = await self._browser.new_context(
            viewport={"width": cfg.viewport_width, "height": cfg.viewport_height},
            user_agent=cfg.user_agent,
            locale=cfg.locale,
            device_scale_factor=1,  # keep screenshot coords == CSS pixel coords
        )
        self._context.set_default_timeout(cfg.action_timeout_ms)
        self._context.set_default_navigation_timeout(cfg.navigation_timeout_ms)
        # Hide the clearest automation fingerprints.
        await self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            "window.chrome = window.chrome || {runtime: {}};"
        )
        self._page = await self._context.new_page()
        logger.info("Browser started (headless=%s)", cfg.headless)
        return self._page

    @property
    def page(self) -> Page:
        if not self._page:
            raise RuntimeError("Browser not started; call start() first")
        return self._page

    async def new_page(self) -> Page:
        """Open an extra page in the shared context (used by the eval harness)."""
        await self.start()
        return await self._context.new_page()

    async def screenshot(self, path: str = "screenshot.png") -> str:
        page = self.page
        await page.screenshot(path=path)
        return path

    async def stop(self) -> None:
        for closer in (self._context, self._browser):
            if closer:
                try:
                    await closer.close()
                except Exception as exc:  # pragma: no cover - best effort
                    logger.debug("Error closing browser resource: %s", exc)
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception as exc:  # pragma: no cover
                logger.debug("Error stopping playwright: %s", exc)
        self._playwright = self._browser = self._context = self._page = None
        logger.info("Browser stopped")


_browser: Optional[Browser] = None


def get_browser() -> Browser:
    global _browser
    if _browser is None:
        _browser = Browser()
    return _browser
