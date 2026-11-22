"""Unit tests for the deterministic DOM matching score and file safety."""

from __future__ import annotations

from app.browser.dom_extractor import DomElement
from app.browser.locator import rule_score
from app.tools.file_tools import _resolve


def _button(**kwargs) -> DomElement:
    defaults = dict(index=0, tag="button", text="", x=0, y=0, w=80, h=30)
    defaults.update(kwargs)
    return DomElement(**defaults)


class TestRuleScore:
    def test_exact_text_match(self):
        assert rule_score(_button(text="登录"), "登录") == 1.0

    def test_case_and_whitespace_insensitive(self):
        assert rule_score(_button(text="  Log In "), "log in") == 1.0

    def test_substring_match(self):
        score = rule_score(_button(text="立即登录系统"), "登录")
        assert 0.5 < score <= 1.0

    def test_aria_label(self):
        assert rule_score(_button(aria_label="提交表单"), "提交表单") == 0.95

    def test_placeholder(self):
        score = rule_score(_button(tag="input", placeholder="搜索商品"), "搜索商品")
        assert score == 0.95

    def test_no_match(self):
        assert rule_score(_button(text="购物车"), "登录") == 0.0

    def test_empty_element_attrs(self):
        assert rule_score(_button(), "任意按钮") == 0.0

    def test_empty_target(self):
        assert rule_score(_button(text="登录"), "") == 0.0


class TestWorkspaceSandbox:
    def test_relative_path_stays_inside(self):
        target = _resolve("notes/result.md")
        assert "workspace" in str(target)
        assert ".." not in target.name

    def test_traversal_is_rejected(self):
        try:
            _resolve("../../etc/passwd")
            raised = False
        except ValueError:
            raised = True
        assert raised, "path traversal must be rejected"
