"""Repeat-call detection in the executor (no LLM, no browser)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent import executor
from app.agent.executor import repeated_calls

STEP_START = {"role": "user", "content": "现在执行步骤 1：做某事"}


def _turn(call_id: str, name: str, arguments: str, output: str) -> list[dict]:
    return [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}]},
        {"role": "tool", "tool_call_id": call_id, "content": output},
    ]


def _three_identical_turns() -> list[dict]:
    return [STEP_START] + _turn("1", "browser_click", '{"target":"登录"}', "ok") \
        + _turn("2", "browser_click", '{"target":"登录"}', "ok") + _turn("3", "browser_click", '{"target":"登录"}', "ok")


class TestRepeatedCalls:
    def test_same_call_same_result_three_times_is_a_loop(self):
        assert repeated_calls(_three_identical_turns(), 3) == ['browser_click({"target":"登录"})']

    def test_two_times_is_still_allowed(self):
        messages = [STEP_START] + _turn("1", "browser_click", "{}", "ok") + _turn("2", "browser_click", "{}", "ok")
        assert repeated_calls(messages, 3) == []

    def test_changing_results_mean_progress_not_a_loop(self):
        messages = [STEP_START]
        for number, output in enumerate(["top", "middle", "bottom"]):
            messages += _turn(str(number), "browser_scroll", '{"direction":"down"}', output)
        assert repeated_calls(messages, 3) == []

    def test_different_arguments_are_different_calls(self):
        messages = [STEP_START] + _turn("1", "browser_click", '{"target":"a"}', "same")
        messages += _turn("2", "browser_click", '{"target":"b"}', "same") + _turn("3", "browser_click", '{"target":"c"}', "same")
        assert repeated_calls(messages, 3) == []

    def test_only_the_current_step_counts(self):
        old = _turn("x1", "browser_click", "{}", "ok") + _turn("x2", "browser_click", "{}", "ok")
        current = _turn("y1", "browser_click", "{}", "ok")
        assert repeated_calls(old + [STEP_START] + current, 3) == []

    def test_a_trimming_notice_is_not_a_step_boundary(self):
        notice = {"role": "user", "content": "(…早期轨迹已省略…)"}
        messages = [STEP_START, notice] + _turn("1", "browser_click", "{}", "ok")
        messages += _turn("2", "browser_click", "{}", "ok") + _turn("3", "browser_click", "{}", "ok")
        assert repeated_calls(messages, 3)


class TestExecutorNudge:
    @pytest.fixture
    def run(self, monkeypatch):
        """Run one executor turn against a scripted LLM; return what it received."""
        seen: dict = {}

        class _Registry:
            def openai_schemas(self):
                return []

        class _LLM:
            async def ask(self, messages, tools=None, **kwargs):
                seen["messages"] = messages
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content="步骤完成", tool_calls=None))])

        monkeypatch.setattr(executor, "get_registry", _Registry)
        monkeypatch.setattr(executor, "get_llm", _LLM)

        async def go(state):
            update = await executor.executor_node(state)
            return update, seen["messages"]

        return go

    @pytest.mark.asyncio
    async def test_loop_gets_one_nudge_that_the_model_sees(self, run):
        update, sent = await run({"messages": _three_identical_turns(), "task": "t", "plan": [], "current_step": 0,
                                  "react_iteration": 3})
        nudges = [m for m in update["messages"] if m.get("role") == "user" and "系统提示" in m["content"]]
        assert len(nudges) == 1
        assert "browser_click" in nudges[0]["content"] and "不要再重复" in nudges[0]["content"]
        assert nudges[0] in sent  # effective now, not only from the next turn on

    @pytest.mark.asyncio
    async def test_nudge_is_not_repeated_once_it_is_in_the_history(self, run):
        nudge = {"role": "user", "content": executor._nudge('browser_click({"target":"登录"})', 3)}
        update, _ = await run({"messages": _three_identical_turns() + [nudge], "task": "t", "plan": [], "current_step": 0,
                               "react_iteration": 4})
        assert not any("系统提示" in str(m.get("content", "")) for m in update["messages"])

    @pytest.mark.asyncio
    async def test_step_kickoff_reaches_the_model_in_the_same_turn(self, run):
        plan = [{"step_index": 1, "description": "打开页面", "status": "pending"}]
        _, sent = await run({"messages": [{"role": "user", "content": "任务"}], "task": "任务", "plan": plan,
                             "current_step": 0, "react_iteration": 0, "verdict_reason": "先确认页面加载完成"})
        assert sent[-1]["content"] == "（Reviewer 提示：先确认页面加载完成）"
        assert sent[-2]["content"] == "现在执行步骤 1：打开页面"
