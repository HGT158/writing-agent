# 项目 Agent 流式编辑工具设计

> 日期：2026-08-11
> 状态：已实现并完成回归（含 v1.14 空白文档首稿生成修复）
> 架构依据：`docs/architecture/phase1-architecture.md` v1.14
> 文档定位：本文件记录 v1.13–v1.14 的专题设计。v1.15 的多会话作用域和 v1.17 的 diff 双视图、事件滑窗契约已在主架构中继续演进；冲突时以 `phase1-architecture.md` 为准。

## 1. 问题

当前项目 Agent 聊天虽然通过 SSE 交付任务事件，但 `chat_project` 会等待模型完整返回 JSON，随后才把整段回复作为一个 `token` 发出，因此用户看不到真实的生成过程。文件修改又依赖模型自行填写 JSON 的 `changes` 数组；模型返回纯回复或空数组时，侧边栏退化为只能聊天。

本次修复参考本地 OpenCode 的三个行为边界：文本以 delta 追加、工具调用具有独立状态、编辑使用精确旧文本/新文本并展示 diff。只借鉴交互和工具协议，不移植 OpenCode 的事件溯源、权限、session part、文件系统或前端框架。

## 2. 目标与非目标

目标：

- 普通项目问答从模型到浏览器真实流式显示。
- 编辑指令由模型调用 `propose_project_edits`，而不是把修改藏在自由文本或 JSON 回复中。
- 工具只生成 pending change set；用户接受前不修改正文。
- 多处建议全量校验后原子创建，失败不留下部分记录。
- 沿用现有助手隔离、项目归属、运行锁、SSE、diff 卡片和 apply/reject API。

非目标：

- 不移植 OpenCode 的 session/event-sourcing 数据模型。
- 不新增数据库表或 change set 状态。
- 不让 Agent 自动接受建议或直接写项目文件。
- 不增加无限工具循环、并行编辑调用、撤销历史或跨项目编辑。
- 不在本次补齐尚未实现的显式多附件 UI。

## 3. 后端设计

### 3.1 项目作用域工具

`agent/tools.py` 新增 `make_project_edit_tool(store, project_id)`，返回一个 `ToolSpec`：

- 名称：`propose_project_edits`
- `idempotent=False`
- `captures_source=False`
- 闭包绑定服务端已经校验的 `project_id`
- `assistant_id` 与 `session_id` 继续由 `ToolContext` 注入

模型参数只有一个 `changes` 数组。每项包含：

- `document_id`
- `old_text`，通常必须非空；仅当目标文档正文为空时允许 `old_text=""`，固定表示在 `[0, 0)` 插入首稿（v1.14）
- `new_text`，允许为空以表示删除
- `document_version`

模型不能提交或覆盖 `assistant_id`、`project_id`、change set 状态和物理路径。

工具先读取并验证全部目标文档。每个 `old_text` 必须在对应版本正文中精确且唯一匹配；工具据此计算 Python Unicode code point 的 `[start, end)`。全部合法后一次调用 `MemoryStore.create_change_sets`，由既有事务再次校验归属、版本和原文快照。工具返回 JSON，包含 `change_set_ids` 和数量，不修改正文文件。

同一次工具调用对每个文档最多包含一项修改。同一文档需要改多处时，模型必须选择覆盖这些位置的一个连续 `old_text`，并给出合并后的 `new_text`。这是现有逐条 apply 契约的必要限制：若同一基准版本产生多条同文档建议，接受第一条后其余建议会因版本递增而失效。

### 3.2 有界流式循环

`AgentRuntime.chat_project` 保留现有 API 签名、助手锁和项目上下文，内部改为：

1. 创建只含 `propose_project_edits` 的本次调用工具表。
2. 第一轮调用 OpenAI-compatible Chat Completions，开启 `stream=True` 并提供工具 schema；系统提示明确要求修改类请求必须调用工具，调用工具时不先输出“已经修改”等说明。
3. 每个可见文本 delta 立即通过 EventBus 发出 `token`。
4. tool-call 参数 delta 只在内存中累积，单次调用上限为 1 MiB；不在 JSON 完整前执行，超限直接失败。
5. 完整参数通过 schema 校验后，发出 `tool_call`，调用一次工具，再发出 `tool_result` 和每个 `change_preview`。
6. 若执行了工具，将 assistant tool-call 与 tool result 加入消息，再进行一个 `tool_choice=none` 的流式说明轮次。
7. 第二轮结束后直接完成任务，不允许模型再次调用工具。若第一轮在工具调用前已经产生可见文本，第二轮首个文本 delta 前补一个段落分隔，避免两段内容粘连。

普通问答若第一轮没有工具调用，第一轮完成即结束。任务结果中的 `reply` 是两个轮次所有可见文本 delta 的顺序拼接；`change_set_ids` 只来自成功的工具结果。

单次第一轮只允许一个 `propose_project_edits` 调用，所有修改放入同一个 `changes` 数组，且每个 `document_id` 最多出现一次。并行、重复工具调用或同文档多项修改作为协议错误失败，避免多个数据库事务破坏整批原子语义和后续 apply 版本冲突。

### 3.3 失败语义

- 流在参数完成前断开：任务失败，不执行工具。
- 参数 JSON 或 schema 非法：任务失败，不创建建议。
- 精确文本零匹配或多匹配：整批失败，不创建建议。
- 文档版本或助手/项目归属冲突：沿用专用冲突异常；正文不变。
- Provider 拒绝流式 tools：明确返回兼容性错误，不进行伪流式回放。
- 工具成功但第二轮说明失败：已创建的 pending change set 和预览保留，任务标记失败并提示用户仍可审核；不得自动拒绝或删除建议。

## 4. 前端设计

`AgentPanel.vue` 为每次发送维护一个当前助手消息。首个 `token` 创建消息，后续 token 追加到该消息内容；工具前后的两个文本轮次仍属于同一次回复，不拆成逐 token 气泡。

面板消费既有事件：

- `tool_call(propose_project_edits)`：显示“Agent 正在准备修改”。
- `tool_result(ok=true)`：显示“修改建议已生成”。
- `tool_result(ok=false)`：显示工具失败原因并保留用户消息。
- `change_preview`：沿用 `ChangeDiff`，接受/拒绝行为不变。
- `task_done` / `task_failed`：结束 sending 状态并关闭流。

v1.13 最初按发起时的 `assistant_id`、`project_id` 和 `document_id` 保护事件作用域。v1.15 引入持久化多会话后，项目聊天改为校验 `assistant_id + project_id + chat_session_id`，切换同项目文档不得丢弃同一会话事件；selection rewrite 仍按文档作用域校验。切换真正的作用域或卸载组件必须关闭 EventSource，旧任务 delta、工具状态和预览不得进入新上下文。

## 5. 测试策略

按风险分级：低风险文案、样式和局部实现可完成后验证；流式状态、工具调用、变更集原子性、版本冲突和作用域隔离等高风险路径，关键测试建议先行（可采用 TDD）。所有实现完成后都要跑针对性测试与全量回归。

1. 后端测试证明多个模型文本 chunk 会产生多个 `token` 事件，并按顺序组成最终 reply。
2. 后端测试证明编辑工具调用创建 pending change set、发出工具状态和预览、正文保持不变。
3. 后端测试覆盖部分参数流中断、非法 JSON、旧文本零/多匹配、同文档重复修改、版本冲突和批量原子失败。
4. API 测试证明既有端点和 SSE 能重放 token/tool/change_preview/终态。
5. 前端测试证明多个 token 只生成一个助手气泡并连续追加。
6. 前端测试覆盖工具状态、diff 保留、失败恢复和旧作用域事件丢弃。

实现完成后依次运行：

```powershell
C:\miniconda\envs\writing-agent\python.exe -m pytest tests\test_memory_isolation.py -q -p no:cacheprovider --basetemp D:\test_agent\pytest-temp-writing-agent
C:\miniconda\envs\writing-agent\python.exe -m pytest tests -q -p no:cacheprovider --basetemp D:\test_agent\pytest-temp-writing-agent
Set-Location web
npm test
npm run typecheck
npm run build
```

## 6. 预计改动范围

- `agent/tools.py`：项目编辑提案 ToolSpec。
- `agent/project_editing.py`：工具输入/结果模型和流式 tool-call 累积结构。
- `agent/llm.py`：OpenAI-compatible 流式文本/tool-call 适配。
- `agent/runtime.py`：有界两轮项目聊天循环。
- `web/src/components/AgentPanel.vue`：文本追加和工具状态。
- 对应 Python、API、Vue 测试。

API 路径、数据库 schema、文档保存/apply 算法和其他 Agent Loop 节点不变。
