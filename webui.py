"""Browser-Agent Web UI backend.

FastAPI + WebSocket:
- POST /api/task          -> start an agent run (one at a time; the browser is
                             a process-wide singleton)
- WS   /ws/{task_id}      -> live state-machine events (planner/executor/
                             tools/reviewer/finish) until the task completes
- GET  /                  -> single-page frontend (web/index.html)
- GET  /screenshots/*     -> screenshots captured during runs
- POST /api/task/stop     -> cancel the running task

Run:  .venv/Scripts/python webui.py   (or run_web.bat)
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.agent.graph import run_agent, setup_registry
from app.config import PROJECT_ROOT, get_config
from app.llm.llm import (
    active_preset_id,
    apply_model_preset,
    build_config_from_preset,
    get_llm,
    reset_llm,
)

logger = logging.getLogger(__name__)

WEB_DIR = PROJECT_ROOT / "web"

app = FastAPI(title="Browser-Agent", docs_url=None, redoc_url=None)

# --- task registry -----------------------------------------------------------


@dataclass
class TaskRecord:
    task_id: str
    task: str
    status: str = "running"  # running | done | error | cancelled
    final_answer: str = ""
    error: str = ""
    created_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    events: list[dict[str, Any]] = field(default_factory=list)
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)


_tasks: dict[str, TaskRecord] = {}
_current: Optional[tuple[TaskRecord, asyncio.Task]] = None


def _screenshot_urls(text: str) -> list[str]:
    """Rewrite workspace screenshot paths in tool output to /screenshots/ URLs."""
    workspace = str(get_config().workspace_root()).replace("\\", "/")
    normalized = text.replace("\\", "/")
    urls = []
    for match in re.finditer(re.escape(workspace) + r"/(\S+?\.png)", normalized):
        name = match.group(1).split("/")[-1]
        urls.append(f"/screenshots/{name}")
    # Also match plain relative mentions like "workspace/xxx.png".
    for match in re.finditer(r"workspace/(\S+?\.png)", normalized):
        name = match.group(1).split("/")[-1]
        url = f"/screenshots/{name}"
        if url not in urls:
            urls.append(url)
    return urls


def _jsonify_event(node: str, update: dict[str, Any]) -> dict[str, Any]:
    """Turn an on_event(node, update) callback into a JSON-safe payload."""
    event: dict[str, Any] = {"node": node, "ts": round(time.time(), 2)}

    if node == "planner" and "plan" in update:
        event["plan"] = update["plan"]
        event["goal"] = update.get("goal", "")

    elif node == "executor" and "messages" in update:
        calls = []
        for message in update["messages"]:
            if message.get("role") == "assistant" and message.get("tool_calls"):
                for call in message["tool_calls"]:
                    calls.append(
                        {
                            "name": call["function"]["name"],
                            "arguments": (call["function"]["arguments"] or "")[:400],
                        }
                    )
            elif message.get("role") == "user":
                event.setdefault("notes", []).append(str(message["content"])[:300])
        event["tool_calls"] = calls

    elif node == "tools" and "messages" in update:
        results = []
        for message in update["messages"]:
            content = str(message.get("content") or "")
            results.append(
                {
                    "output": content[:1200],
                    "screenshots": _screenshot_urls(content),
                }
            )
        event["tool_results"] = results
        if update.get("terminated"):
            event["terminated"] = True

    elif node == "reviewer":
        for key in ("verdict", "verdict_reason", "current_step", "final_answer"):
            if update.get(key) not in (None, ""):
                event[key] = update[key]
        if "plan" in update:
            event["plan"] = update["plan"]

    elif node == "finish":
        event["final_answer"] = update.get("final_answer", "")

    # Keep the plan fresh on every event that carries it.
    if "plan" in update and "plan" not in event:
        event["plan"] = update["plan"]
    if update.get("final_answer") and not event.get("final_answer"):
        event["final_answer"] = update["final_answer"]
    return event


async def _run_agent_task(record: TaskRecord) -> None:
    """Background coroutine: run the agent and stream events into the queue."""

    async def on_event(node: str, update: dict[str, Any]) -> None:
        event = _jsonify_event(node, update)
        record.events.append(event)
        record.queue.put_nowait(event)

    try:
        result = await run_agent(record.task, on_event=on_event)
        record.final_answer = result.get("final_answer", "") or "（无最终答案）"
        record.status = "done"
    except asyncio.CancelledError:
        record.status = "cancelled"
        record.error = "任务已取消"
        raise
    except Exception as exc:
        logger.exception("agent run crashed")
        record.status = "error"
        record.error = f"{type(exc).__name__}: {exc}"
    finally:
        record.finished_at = time.time()
        record.queue.put_nowait({"node": "__end__", "status": record.status,
                                 "final_answer": record.final_answer, "error": record.error})


# --- routes -------------------------------------------------------------------

@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/config")
async def api_config():
    cfg = get_config()
    active = get_llm().config  # reflects the currently selected preset
    return {
        "model": active.model,
        "vision_model": active.vision_model,
        "api_key_set": bool(active.api_key),
        "sandbox_provider": cfg.sandbox.provider,
        "mcp_servers": cfg.mcp.servers if cfg.mcp.enabled else [],
    }


@app.get("/api/models")
async def api_models():
    """List switchable model presets + which one is active."""
    presets = []
    for preset in get_config().model_presets:
        merged = build_config_from_preset(preset)
        presets.append(
            {
                "id": preset.id,
                "label": preset.label or preset.model,
                "model": merged.model,
                "vision_model": merged.vision_model,
                "base_url": merged.base_url,
                "api_key_set": bool(merged.api_key),
            }
        )
    return {
        "active": active_preset_id() or "default",
        "default_model": get_config().llm.model,
        "presets": presets,
    }


@app.post("/api/model")
async def api_switch_model(body: dict[str, Any]):
    if _current is not None and not _current[1].done():
        raise HTTPException(409, "任务运行中，不能切换模型")
    preset_id = str(body.get("id", ""))
    if preset_id in ("", "default"):
        cfg = reset_llm(None)
        return {"active": "default", "model": cfg.model, "api_key_set": bool(cfg.api_key)}
    preset = next((p for p in get_config().model_presets if p.id == preset_id), None)
    if preset is None:
        raise HTTPException(404, f"未知模型预设: {preset_id}")
    cfg = apply_model_preset(preset)
    return {"active": preset.id, "model": cfg.model, "api_key_set": bool(cfg.api_key)}


@app.get("/api/tasks")
async def api_tasks():
    return [
        {
            "task_id": t.task_id,
            "task": t.task,
            "status": t.status,
            "final_answer": t.final_answer[:500],
            "error": t.error[:300],
            "created_at": t.created_at,
        }
        for t in reversed(list(_tasks.values()))
    ][:50]


@app.post("/api/task")
async def api_create_task(body: dict[str, Any]):
    global _current
    task = str(body.get("task", "")).strip()
    if not task:
        raise HTTPException(400, "task is required")
    if _current is not None and not _current[1].done():
        raise HTTPException(409, "已有任务在运行中，请等待完成或先取消")
    if not get_config().llm.api_key:
        raise HTTPException(400, "未配置 API Key（环境变量 MIMO_API_KEY 或 config.toml）")

    record = TaskRecord(task_id=uuid.uuid4().hex[:12], task=task)
    _tasks[record.task_id] = record
    asyncio.create_task(setup_registry())  # warm up tools/MCP while WS connects
    runner = asyncio.create_task(_run_agent_task(record), name=f"task-{record.task_id}")
    _current = (record, runner)
    return {"task_id": record.task_id}


@app.post("/api/task/stop")
async def api_stop_task():
    global _current
    if _current is None or _current[1].done():
        return {"stopped": False}
    _current[1].cancel()
    return {"stopped": True}


@app.websocket("/ws/{task_id}")
async def ws_events(websocket: WebSocket, task_id: str):
    record = _tasks.get(task_id)
    if record is None:
        await websocket.close(code=4404, reason="unknown task")
        return
    await websocket.accept()
    try:
        # Replay events already produced (fast planners may have queued some
        # before the WS connected), then stream live.
        for event in record.events:
            await websocket.send_json(event)
        while True:
            event = await record.queue.get()
            await websocket.send_json(event)
            if event.get("node") == "__end__":
                break
    except WebSocketDisconnect:
        logger.debug("WS disconnected for task %s", task_id)
    except Exception as exc:  # pragma: no cover
        logger.warning("WS error: %s", exc)


# Screenshot file service (browser_screenshot tool writes into workspace/).
app.mount("/screenshots", StaticFiles(directory=str(get_config().workspace_root())), name="screenshots")


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    host, port = "127.0.0.1", 8686
    print(f"\n  Browser-Agent Web UI  →  http://{host}:{port}\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
