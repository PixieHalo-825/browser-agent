"""Planner node: decompose the task into an ordered plan, or revise it."""

from __future__ import annotations

import logging
from typing import Any

from app.agent.state import AgentState
from app.config import get_config
from app.llm.llm import get_llm
from app.prompt import planner as prompts

logger = logging.getLogger(__name__)


def _normalize_plan(raw: Any) -> list[dict]:
    """Coerce LLM output into a clean step list."""
    steps: list[dict] = []
    if isinstance(raw, dict):
        raw = raw.get("plan", [])
    if not isinstance(raw, list):
        raise ValueError(f"plan is not a list: {raw!r}")
    for i, item in enumerate(raw, 1):
        if isinstance(item, str):
            description = item
        elif isinstance(item, dict):
            description = str(item.get("description") or item.get("step") or "").strip()
        else:
            continue
        if not description:
            continue
        steps.append(
            {
                "step_index": len(steps) + 1,
                "description": description,
                "status": "pending",
                "result": "",
            }
        )
    if not steps:
        raise ValueError("empty plan")
    cfg = get_config().agent
    if len(steps) > cfg.max_plan_steps:
        logger.warning("Plan truncated from %d to %d steps", len(steps), cfg.max_plan_steps)
        steps = steps[: cfg.max_plan_steps]
    return steps


def plan_status_text(plan: list[dict]) -> str:
    icons = {"pending": "○", "in_progress": "◐", "done": "●", "failed": "✗"}
    lines = []
    for step in plan:
        icon = icons.get(step.get("status", "pending"), "○")
        line = f"{icon} 步骤{step['step_index']}: {step['description']}"
        if step.get("result"):
            line += f"\n   └ 结果: {step['result'][:200]}"
        lines.append(line)
    return "\n".join(lines)


async def planner_node(state: AgentState) -> dict:
    cfg = get_config().agent
    llm = get_llm()
    task = state["task"]

    if not state.get("plan"):
        # ---- initial planning --------------------------------------------
        logger.info("[Planner] generating plan for: %s", task)
        result = await llm.ask_json(
            [
                {"role": "system", "content": prompts.PLAN_SYSTEM.format(max_steps=cfg.max_plan_steps)},
                {"role": "user", "content": f"用户任务：{task}"},
            ]
        )
        plan = _normalize_plan(result)
        goal = str(result.get("goal") or task) if isinstance(result, dict) else task
        logger.info("[Planner] plan with %d steps", len(plan))
        return {
            "plan": plan,
            "goal": goal,
            "current_step": 0,
            "react_iteration": 0,
            "verdict": "",
            "step_notes": [f"[Planner] 计划生成（{len(plan)} 步）：\n" + plan_status_text(plan)],
        }

    # ---- replanning ------------------------------------------------------
    review_note = state.get("verdict_reason", "")
    messages = [
        {
            "role": "system",
            "content": prompts.REPLAN_SYSTEM.format(
                task=task,
                plan_status=plan_status_text(state["plan"]),
                review_note=review_note,
                replans=state.get("replans", 0),
                max_replans=cfg.max_replans,
            ),
        },
        {"role": "user", "content": f"请修订计划以完成剩余任务。已完成步骤的成果会在执行轨迹中保留。"},
    ]
    result = await llm.ask_json(messages)
    plan = _normalize_plan(result)
    logger.info("[Planner] replanned to %d steps", len(plan))
    return {
        "plan": plan,
        "current_step": 0,
        "react_iteration": 0,
        "verdict": "",
        "replans": state.get("replans", 0) + 1,
        "step_notes": [f"[Planner] 重规划（第 {state.get('replans', 0) + 1} 次，剩余 {len(plan)} 步）：\n" + plan_status_text(plan)],
    }
