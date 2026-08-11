# 文档优先工作台与选区局部改写设计（已由 v1.9 规格取代）

日期：2026-08-10

本文件记录 v1.8 的早期范围，已由
`docs/superpowers/specs/2026-08-10-writing-ide-project-workspace-design.md`
取代，不再作为阶段 4 实施依据。

本设计是架构 v1.8 中阶段 4 范围的实施说明。若本文件与
`docs/phase1-architecture.md` 冲突，始终以架构文档为准。

## 目标

将 Web UI 的主对象从聊天会话改为文章草稿。用户可以在文章中选择一段
Markdown 文本，输入改写要求，查看 AI 的替换建议，并明确决定是否应用。

局部改写不应替代完整写作 Agent。它是受限的编辑意图：默认使用当前助手的
persona 和 editing Skill，只处理用户选区及其必要上下文，且复用既有
AgentRuntime、EventBus、MemoryStore、AssistantRegistry 和运行锁。

## 非目标

- 不实现完整文档历史、多人协作或跨设备同步。
- 不把前端变成通用聊天或角色扮演界面。
- 不允许前端直接访问 LLM、SQLite 或 `data/assistants/`。
- 不在本阶段改变跨助手隔离、MCP、Skill 或 Scheduler 的既有语义。

## 交互

1. 用户打开属于当前助手的文章草稿，在 CodeMirror 中选择连续文本。
2. 选区附近显示悬浮工具栏；用户输入改写指令并发起生成。
3. 前端发送文章 id、助手 id、选区文本与范围、指令、当前文档版本。
4. 后端生成一个待确认建议，并通过 SSE 发送 `rewrite_preview`。
5. 前端展示原文和建议文本的 diff，用户可接受、拒绝或重新生成。
6. 只有接受操作会更新正文；更新成功后文档版本号递增。

编辑器必须把选区保存在自身状态中，不能依赖浏览器当前 Selection；这样点击
工具栏输入框后，选区范围仍可用于预览和应用。

## 数据与并发

文章的当前状态包含 `assistant_id`、`article_id`、Markdown 内容和单调递增的
`document_version`。局部建议记录 `rewrite_id`、文章/助手归属、选区范围、
原文快照、替换文本、基准版本和状态（pending、applied、rejected、expired）。

创建建议与应用建议都必须通过 MemoryStore。应用时在单一 SQLite 事务内验证：

- 建议、文章与请求具有相同的 `assistant_id`；
- 建议仍是 pending；
- 当前版本等于建议的基准版本和请求的期望版本；
- 当前范围的文本仍等于建议保存的原文快照。

任何验证失败返回冲突，不更新正文。客户端重新读取文档后才可以再次生成建议。

## API 与事件

`POST /api/articles/{article_id}/selection-rewrites` 创建局部改写任务。请求中
`assistant_id`、`selected_text`、`selection_range`、`instruction` 和
`document_version` 均为必填。响应含 `task_id` 与 `rewrite_id`。

任务 SSE 流继续使用 `GET /api/tasks/{task_id}/stream`。生成成功时发送：

```json
{
  "type": "rewrite_preview",
  "data": {
    "rewrite_id": "...",
    "article_id": "...",
    "range": {"from": 10, "to": 24},
    "replacement": "建议替换文本",
    "document_version": 7
  }
}
```

`POST /api/articles/{article_id}/selection-rewrites/{rewrite_id}/apply` 显式应用
建议；`POST .../reject` 拒绝建议。两个请求均须带 `assistant_id`，apply 还须带
期望的 `document_version`。

## 验收标准

- 文章列表、读取、保存、局部改写创建、应用和拒绝均按 `assistant_id` 隔离。
- 同一助手有运行中任务时，局部改写按既有 409 锁语义拒绝；不同助手不互相阻塞。
- 生成建议不会修改文章；只有 apply 成功后正文和版本号才改变。
- 文本或版本冲突时 apply 返回 409，且文章内容保持不变。
- 前端选区工具栏、diff、接受、拒绝和重新生成可用；切换助手会切换文章命名空间。
- `tests/test_memory_isolation.py` 与全量测试在新增覆盖后保持通过。
