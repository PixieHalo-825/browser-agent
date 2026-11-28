# 架构设计文档

本项目参考 [OpenManus](https://github.com/FoundationAgents/OpenManus) 的设计（BaseAgent → ReActAgent → ToolCallAgent 的 ReAct 层次、PlanningFlow 规划流、BaseTool 工具抽象、MCP 接入），并用 LangGraph 将"单体 Agent 循环"重构为显式的**分层状态机**。本文档说明四个核心子系统的设计决策。

## 1. 分层状态机（`app/agent/`）

### 为什么不用单体 Chain / 单 Agent 循环

单体 ReAct 循环（OpenManus 的 `Manus` 或 LangChain AgentExecutor）把"想做什么、怎么做、做得对不对"压在一个提示词里：长链路任务（>10 步）上下文膨胀、中途偏航无法回头、失败只能整轮重试。本项目将三种认知职责拆到三个节点：

| 节点 | 职责 | LLM 输出 |
|------|------|---------|
| **Planner** | 任务拆解为有序步骤；失败后重规划（保留已成功成果） | 结构化 JSON 计划 |
| **Executor** | 对当前步骤跑 ReAct 工具循环（think → tool → observe） | tool_calls / 文本结论 |
| **Reviewer** | 审查步骤结果，判定 continue / replan / finish | 结构化 JSON 判定 |

### 状态与拓扑（`app/agent/state.py` / `graph.py`）

`AgentState` 是 LangGraph TypedDict：`messages`（ReAct 轨迹，add reducer 累积）+ `plan`（步骤列表）+ `current_step` + `verdict` 等控制位。

```
START → planner → executor ⇄ tools → reviewer
                       │                │
                       │ terminate      ├─ continue → executor（下一步）
                       ▼                ├─ replan   → planner（重规划，上限 3 次）
                      END               └─ finish   → END（输出 final_answer）
```

关键路由规则：

- **executor → tools**：assistant 消息带 tool_calls；**executor → reviewer**：纯文本结论（步骤完成）或达到单步 ReAct 轮数上限（强制审查，防死循环）
- **tools → END**：`terminate` 工具被调用，其 summary 直接成为 final_answer（短路路径，无需再过 Reviewer）
- **reviewer → planner**：仅当 replan 次数未超上限；超限强制 finish 并说明原因
- `MemorySaver` checkpoint + `run_agent` 外层 super-step 上限（60）双保险防失控

与 OpenManus 的对应关系：`PlanningFlow`（executor + replan 节点）→ Planner 节点；`ToolCallAgent.think/act` → Executor ⇄ Tools 循环；OpenManus 无显式 Reviewer（靠 terminate）→ 本项目补上审查层获得步骤级纠错能力。

### 长链路任务的稳定性设计

- 步骤结果写回 `plan[i].result`，Planner 重规划与 Executor system prompt 都能看到已完成成果——跨步骤记忆不靠 messages 硬撑
- messages 超过 60 条时保留头尾 + 截断标记（`executor._trim`），控制上下文成本
- 每步 ReAct 上限 8 轮、重规划上限 3 次、总 super-step 上限 60——三层兜底
- 循环检测（`executor.repeated_calls`）：同一工具调用在同一 step 内以相同参数连续 3 次得到相同结果，即向模型注入一条提示，要求换参数、换工具或结束本步骤；结果在变化（如滚动翻页）的重复调用不算循环

## 2. GUI-Plus 多模态双重定位（`app/browser/locator.py`）

### 问题

动态网页的 DOM 不可靠：图标按钮无文本/aria；canvas/重度混淆页面 DOM 中根本无元素；同文本按钮需要视觉上下文区分；反爬验证页语义信息被刻意抹除。纯 DOM 定位（browser-use 等的默认路径）在这些场景大量失败。

### 四级降级流水线

```
locate(page, "购物车图标")
  │
  ├─① dom-rule   规则打分：text/aria/placeholder/name/id 精确→包含匹配（无 LLM，毫秒级）
  │    低于阈值 ↓
  ├─② dom-llm    文本 LLM 从编号 DOM 候选列表中选择（处理模糊/同义描述）
  │    无候选或拒绝 ↓
  ├─③ vision-som 截图 + Set-of-Mark 标注（候选元素画编号框）→ VLM 返回编号
  │    无 DOM 候选（canvas 等）↓
  └─④ vision-ground VLM 直接回归目标 bbox → mouse.click(bbox 中心)
```

- 每级记录 `channel` + `confidence`，评测按通道统计成功率（`eval/`）
- DOM 通道命中优先走 Playwright 元素点击（完整 actionability 检查）；handle 因 DOM 重渲染失效时回退到坐标点击（`app/browser/actions.py`）
- 落点校验（`locator.hit_test`）：定位结果交给动作之前，先看这一下会点在哪。DOM 目标先滚入视口并取最新中心；坐标在视口外、落点上什么都没有、或纯视觉坐标落在空白背景上时，判为定位失败让智能体换个说法重试（任务本来就是“点空白处”的除外）。DOM 目标被别的元素盖住时不拦截——自定义控件常在真实控件上盖一层装饰，点在装饰层上正是它的工作方式——只在工具结果里附一句提示让智能体核对效果
- DOM 提取器（`dom_extractor.py`）递归穿透 shadow DOM，给元素注入 `data-ba-idx` 标记用于回查；视觉通道坐标与视口像素严格 1:1（`device_scale_factor=1`，SoM 标注图缩放后坐标逆映射）

## 3. 安全沙箱（`app/sandbox/`）

统一 `SandboxProvider` 接口（`run(code, timeout) -> ExecutionResult`），三种实现：

| 实现 | 隔离手段 | 选择条件 |
|------|---------|---------|
| `DaytonaSandbox` | 云端沙箱（daytona-sdk `code_run`），实例复用 | `daytona-sdk` 已安装且有 API key |
| `DockerSandbox` | 每次执行临时容器：`network_mode=none`、`read_only=True` + `/tmp` tmpfs、`mem_limit=512m`（同时禁用 swap，上限是硬的）、`nano_cpus` 上限、超时 kill；代码 base64 编码注入避免转义攻击 | Docker 引擎可达；Docker Desktop 已安装但没开时会被自动启动 |
| `LocalSubprocessSandbox` | `python -I`（隔离模式）子进程 + 超时 + 临时目录，**无网络/文件系统隔离** | 兜底（启动时大声警告） |

选择逻辑在 `manager.py`：`provider=auto` 时 Daytona → Docker → Local 逐级探测降级；显式指定则严格失败。Docker 探测失败而本机装有 Docker Desktop 时，先自动把它启动起来并等待引擎就绪（`auto_start_docker`，默认最多 90 秒，实测约 18 秒），仍未就绪才降级——重启电脑后忘记打开 Docker 不会再让沙箱悄悄失去隔离。Docker SDK 是同步的，全部调用经 `asyncio.to_thread` 包装并加外层超时保护。

## 4. MCP 工具标准化（`app/mcp/` + `mcp_servers/`）

### 动机

OpenManus 的工具是进程内 Python 类：新增工具要改 agent 代码、重启进程；工具与 agent 的故障域、依赖、生命周期全部耦合。MCP 把工具变成**独立进程/容器**：

```
        ToolRegistry（统一 function-calling schema）
              │
   ┌──────────┼──────────────┬─────────────────┐
   │ stdio    │ stdio        │ streamable-http │  ← 传输层对 Executor 透明
   ▼          ▼              ▼
 filesystem  search       docker compose 拉起的
 server.py   server.py    容器化 server (Dockerfile.mcp)
```

### 实现

- **MCPConnection**（`client.py`）：官方 SDK 的 `stdio_client`/`ClientSession` 是 anyio task-group context manager，必须同一 task 内进出——每个连接跑一个专属 lifecycle task 持有会话，工具调用经 asyncio 队列转发（串行化，匹配 Executor 的顺序执行模型）
- **MCPToolAdapter**：把远端工具适配成本地 `BaseTool`（工具名加 `mcp_{server}_{tool}` 前缀防冲突），schema 直接透传
- **热插拔**：`config.toml [mcp] servers` 列表增删 + `[mcp_servers.X]` 定义（command/args 或 url），agent 代码零改动；连接失败不影响其余工具
- 兼容 mcp 1.x（FastMCP）与 2.x（MCPServer）两代 API

## 5. 评测设计（`eval/`）

- **可复现**：全部场景为本地 `file://` HTML，不依赖外网与第三方页面改版
- **判真实效果**：每个任务以点击后的 `window.__clicked` 是否等于期望值判定，而非定位器自报成功
- **对照模式**：`--mode dom-only`（视觉通道关闭）vs `gui-plus`（完整流水线），同一批任务双跑
- 输出分场景/分通道统计（dom-rule / dom-llm / vision-som / vision-ground / failed）与 markdown 报告

## 6. 工程决策记录

| 决策 | 理由 |
|------|------|
| 消息用原生 OpenAI dict 而非 langchain 消息类 | 避免双重抽象与转换损耗；LangGraph state 不要求 langchain 类型 |
| system prompt 不进 state.messages，每轮重建 | 计划状态（当前步骤、已完成结果）实时反映，无需消息修补 |
| 视觉模型与文本模型分离配置 | grounding 任务可指向专用 VLM 端点，成本/质量独立调优 |
| 搜索源按顺序自动切换（`app/tools/search_providers.py`：Bing 网页 → DuckDuckGo → 百炼搜索接口） | 单一搜索源被限流或屏蔽不再让搜索工具失效；前两者零 key，开箱即跑。实测 DuckDuckGo 库在大陆网络下完全不可用，Bing 网页 0.4 秒返回 |
| 浏览器工具默认进程内 + MCP browser server 可选 | 进程内单例避免跨进程浏览器状态同步；MCP 化用于跨 agent 复用场景 |
