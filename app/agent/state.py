"""Agent state shared by every node of the LangGraph state machine."""

from __future__ import annotations

import operator
from typing import Annotated, List, TypedDict

# Plan step: {"step_index": int, "description": str,
#             "status": "pending"|"in_progress"|"done"|"failed", "result": str}


class AgentState(TypedDict, total=False):
    # ReAct conversation (assistant / tool / user messages; system prompts are
    # rebuilt on every executor turn, so they are never stored here).
    messages: Annotated[List[dict], operator.add]

    task: str                      # original user request
    goal: str                      # planner's restatement of the goal
    plan: list[dict]               # full step list (overwritten on (re)plan)
    current_step: int              # index into plan currently being executed
    react_iteration: int           # tool-call round within the current step

    verdict: str                   # reviewer verdict: continue | replan | finish
    verdict_reason: str
    replans: int                   # how many times the plan was revised

    terminated: bool               # set when the terminate tool fires
    final_answer: str              # delivered on finish
    step_notes: Annotated[List[str], operator.add]  # human-readable run log
