"""DOM channel of the GUI-Plus locator.

Extracts interactive elements from the page into a numbered, structured list.
Each element gets a ``data-ba-idx`` marker attribute so later actions can
re-resolve it by index. Shadow DOM is walked recursively; elements rendered
inside canvas/iframe remain invisible to this channel (the vision channel
exists precisely for them).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from playwright.async_api import Page

from app.config import get_config

logger = logging.getLogger(__name__)

# Marker attribute injected on extracted elements.
MARKER_ATTR = "data-ba-idx"

# Single JS pass: collects visible interactive elements (recursing into shadow
# roots), sorts them by visual position, then re-tags markers so that marker
# indices match the sorted output order.
_COLLECT_AND_TAG_JS = """
() => {
  const SELECTOR = 'a, button, input, select, textarea, summary, [role], [onclick], [contenteditable="true"], [tabindex]:not([tabindex="-1"])';
  const out = [];
  const refs = [];

  const isVisible = (el) => {
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 1 && rect.height > 1;
  };

  const walk = (root, inShadow) => {
    for (const el of root.querySelectorAll(SELECTOR)) {
      if (isVisible(el)) {
        const rect = el.getBoundingClientRect();
        const text = (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ');
        out.push({
          tag: el.tagName.toLowerCase(),
          role: el.getAttribute('role') || '',
          ariaLabel: el.getAttribute('aria-label') || '',
          placeholder: el.getAttribute('placeholder') || '',
          name: el.getAttribute('name') || '',
          id: el.id || '',
          text: text,
          href: (el.tagName === 'A' ? (el.getAttribute('href') || '') : ''),
          value: (el.value !== undefined && el.value !== null && el.value !== '' ? String(el.value) : ''),
          type: el.getAttribute('type') || '',
          x: Math.round(rect.x), y: Math.round(rect.y),
          w: Math.round(rect.width), h: Math.round(rect.height),
          inShadow: inShadow,
        });
        refs.push(el);
      }
      if (el.shadowRoot) walk(el.shadowRoot, true);
    }
  };

  walk(document, false);
  // Pair descriptions with element handles, sort by visual position, re-tag.
  const pairs = out.map((desc, i) => ({ desc, el: refs[i] }));
  pairs.sort((a, b) => (a.desc.y - b.desc.y) || (a.desc.x - b.desc.x));
  pairs.forEach((p, i) => p.el.setAttribute('data-ba-idx', String(i)));
  return pairs.map((p) => p.desc);
}
"""


@dataclass
class DomElement:
    index: int
    tag: str
    text: str = ""
    role: str = ""
    aria_label: str = ""
    placeholder: str = ""
    name: str = ""
    id: str = ""
    href: str = ""
    value: str = ""
    type: str = ""
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    in_shadow: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def center(self) -> tuple[int, int]:
        return (self.x + self.w // 2, self.y + self.h // 2)

    @property
    def bbox(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.x + self.w, self.y + self.h)

    def label(self, max_text: int = 60) -> str:
        """Human/LLM-readable one-line description."""
        parts = [f"[{self.index}] <{self.tag}>"]
        attrs = []
        if self.role:
            attrs.append(f"role={self.role}")
        if self.aria_label:
            attrs.append(f"aria={self.aria_label}")
        if self.placeholder:
            attrs.append(f"placeholder={self.placeholder}")
        if self.name:
            attrs.append(f"name={self.name}")
        if self.id:
            attrs.append(f"id={self.id}")
        if self.href:
            attrs.append(f"href={self.href[:40]}")
        if self.value:
            attrs.append(f"value={self.value[:20]}")
        if self.in_shadow:
            attrs.append("in-shadow-dom")
        if attrs:
            parts.append("(" + ", ".join(attrs) + ")")
        if self.text:
            text = self.text[:max_text]
            parts.append(f": {text!r}")
        parts.append(f"@({self.x},{self.y},{self.w}x{self.h})")
        return " ".join(parts)


async def extract_interactive_elements(page: Page) -> list[DomElement]:
    """Extract and marker-tag visible interactive elements on the current page."""
    cfg = get_config().dom
    raw = await page.evaluate(_COLLECT_AND_TAG_JS)
    elements: list[DomElement] = []
    for item in raw:
        elements.append(
            DomElement(
                index=len(elements),
                tag=item["tag"],
                text=(item["text"] or "")[: cfg.max_text_length],
                role=item["role"],
                aria_label=item["ariaLabel"],
                placeholder=item["placeholder"],
                name=item["name"],
                id=item["id"],
                href=item["href"],
                value=item["value"],
                type=item["type"],
                x=item["x"],
                y=item["y"],
                w=item["w"],
                h=item["h"],
                in_shadow=item["inShadow"],
            )
        )
    if len(elements) > cfg.max_elements:
        logger.debug("Truncating %d elements to %d", len(elements), cfg.max_elements)
        elements = elements[: cfg.max_elements]
    logger.debug("Extracted %d interactive elements", len(elements))
    return elements


def format_elements(elements: list[DomElement], max_text: Optional[int] = None) -> str:
    """Render the numbered element list for LLM consumption."""
    if not elements:
        return "(no interactive elements found)"
    return "\n".join(e.label(max_text) if max_text else e.label() for e in elements)


async def find_by_marker(page: Page, index: int) -> Optional[Any]:
    """Re-resolve a previously extracted element by its marker index.

    Searches the light DOM and all shadow roots.
    """
    selector = f"[{MARKER_ATTR}='{index}']"
    handle = await page.query_selector(selector)
    if handle:
        return handle
    # Try shadow roots (query_selector does not pierce them).
    return await page.evaluate_handle(
        """(idx) => {
            const find = (root) => {
                for (const el of root.querySelectorAll('*')) {
                    if (el.getAttribute && el.getAttribute('data-ba-idx') === String(idx)) return el;
                    if (el.shadowRoot) { const hit = find(el.shadowRoot); if (hit) return hit; }
                }
                return null;
            };
            return find(document);
        }""",
        index,
    )
