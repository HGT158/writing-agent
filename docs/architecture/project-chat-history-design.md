# 项目 Agent 多会话历史设计

> 日期：2026-08-11
> 状态：已实现并完成回归（Python 156/156、记忆隔离 10/10、前端 41/41，类型检查与生产构建通过）
> 架构基线：`docs/architecture/phase1-architecture.md` v1.14
> 文档定位：本文件冻结记录 v1.15 多会话历史的设计与当时测试基线，不是当前架构的单一事实来源。v1.17 已加入上下文分层压缩和 diff 双视图；现行契约以 `phase1-architecture.md` 为准。

## 1. 问题

项目 Agent 当前只把消息保存在 `AgentPanel.vue` 的组件内存中。刷新页面、重新打开项目、切换文档或组件重新挂载都会丢失对话；`AgentRuntime.chat_project` 每次又生成一次性 `session_id`，既不写入项目聊天消息，也不读取旧消息，因此模型无法延续同一项目会话。

聊天生成的 change preview 还同时保存在 AgentPanel 与 DocumentEditor。若用户在编辑器中的副本点击接受，DocumentEditor 只清除自身副本，侧栏副本仍然存在，造成“接受成功但卡片不消失”。

## 2. 目标与非目标

目标：

- 每个助手的每个项目可拥有多个独立聊天会话。
- 打开项目时默认恢复最近更新的会话；用户可新建、切换和删除会话。
- 同项目切换文档不切换会话，也不清空消息。
- 历史消息持久化后全部传给模型，保证会话连续性。
- 会话详情同时恢复该会话尚未处理的 chat change set。
- 存在 pending change set 的会话禁止删除。
- 接受或拒绝 change set 成功后，对应卡片立即且唯一地消失。
- 所有数据访问强制按 `assistant_id + project_id + chat_session_id` 隔离。

非目标：

- 本次不实现上下文压缩、摘要、token 预算或历史截断。
- 不实现会话重命名、搜索、导出、分支、分享或跨项目移动。
- 不迁移或混用普通 Agent Loop 的 `sessions/messages`。
- 不回填功能上线前仅存在于浏览器内存中的项目聊天。
- 不改变 selection rewrite 的 change set 与编辑器预览流程。

## 3. 方案选择

采用专用项目聊天表，不扩展普通 Agent Loop 的通用 `sessions/messages`，也不使用浏览器本地存储。专用表保持项目归属、删除语义和模型上下文清晰，同时避免影响 CLI/LangGraph checkpoint 与现有 FTS 记忆召回。

SQL 只位于 `memory/`。新增聚焦的 `memory/project_chat.py`，由 `MemoryStore` 提供强制带 `assistant_id` 的公开方法；`agent/` 与 `api/` 不直接写 SQL。

## 4. 数据模型

### 4.1 `project_chat_sessions`

字段：

- `assistant_id TEXT NOT NULL`
- `project_id TEXT NOT NULL`
- `chat_session_id TEXT NOT NULL`
- `title TEXT NOT NULL`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`
- 主键：`(assistant_id, project_id, chat_session_id)`

会话列表按 `updated_at DESC, chat_session_id DESC` 返回。新会话内部初始标题为“新对话”；写入第一条用户消息时，使用去除首尾空白后的首段文本生成标题，最大 80 个 Unicode code point。后续消息不自动改标题。

### 4.2 `project_chat_messages`

字段：

- `message_id INTEGER PRIMARY KEY AUTOINCREMENT`
- `assistant_id TEXT NOT NULL`
- `project_id TEXT NOT NULL`
- `chat_session_id TEXT NOT NULL`
- `role TEXT NOT NULL`，仅允许 `user` / `assistant`
- `content TEXT NOT NULL`
- `created_at TEXT NOT NULL`

查询固定按 `message_id ASC` 返回完整历史。每个查询和写入都必须同时校验助手、项目与会话归属。表建立 `(assistant_id, project_id, chat_session_id, message_id)` 索引。

### 4.3 Change set 关联

现有 `change_sets.session_id` 直接保存 `chat_session_id`，不新增 change set 外键或状态。恢复会话时只查询同时满足以下条件的记录：

- `assistant_id`、`project_id`、`session_id` 与当前会话一致；
- `source = 'chat'`；
- `status = 'pending'`。

删除会话时先在同一事务中检查 pending chat change set。若存在则返回冲突；否则删除会话消息、会话记录及该会话已 applied/rejected 的 chat change set 元数据。已经应用到项目文件的正文不回滚。

## 5. MemoryStore 契约

新增以下项目聊天入口，所有签名以 `assistant_id` 开头：

- `create_project_chat_session(assistant_id, project_id) -> ProjectChatSessionRecord`
- `list_project_chat_sessions(assistant_id, project_id) -> list[ProjectChatSessionRecord]`
- `get_project_chat_session(assistant_id, project_id, chat_session_id) -> ProjectChatSessionRecord`
- `list_project_chat_messages(assistant_id, project_id, chat_session_id) -> list[ProjectChatMessageRecord]`
- `add_project_chat_message(assistant_id, project_id, chat_session_id, role, content) -> ProjectChatMessageRecord`
- `list_pending_chat_changes(assistant_id, project_id, chat_session_id) -> list[ChangeSetRecord]`
- `delete_project_chat_session(assistant_id, project_id, chat_session_id) -> None`

创建、读取和删除前都通过项目表验证项目活动状态及助手归属。添加第一条用户消息时，在同一事务中写消息、生成标题并更新 `updated_at`；添加后续消息只更新时间。项目 purge 与助手 purge 流程都必须级联清理两张新表；项目归档保留历史，但归档后所有项目聊天 API 均不可访问。

## 6. API 契约

新增：

- `GET /api/projects/{project_id}/agent/sessions?assistant_id=...`
- `GET /api/projects/{project_id}/agent/sessions/{chat_session_id}?assistant_id=...`
- `DELETE /api/projects/{project_id}/agent/sessions/{chat_session_id}?assistant_id=...`

列表项包含 `chat_session_id`、`title`、`created_at`、`updated_at` 和 `message_count`。详情响应包含：

- `session`
- 按正序排列的 `messages`
- 转换为现有 `ChangePreview` 形状的 `pending_changes`

现有 `POST /api/projects/{project_id}/agent/messages` 请求新增可空 `chat_session_id`：

- 为空时，服务端同步创建会话后再启动 TaskBroker；
- 非空时，服务端在返回 202 前校验会话归属；
- 响应改为 `{ "task_id": "...", "chat_session_id": "..." }`。

任务提交前仍校验助手、运行锁、项目、会话及可选的当前文档；不得先入队再异步暴露归属错误。删除存在 pending diff 的会话返回 HTTP 409；不存在或跨作用域资源统一返回 404。

## 7. Runtime 与模型上下文

`AgentRuntime.chat_project` 接收已校验的 `chat_session_id`，不再为每条消息生成一次性项目聊天 session id。

单次调用顺序：

1. 获取助手运行锁并重新验证项目、会话和当前文档归属。
2. 持久化当前用户消息；第一条用户消息同时更新会话标题。
3. 读取该会话全部可见 `user/assistant` 历史，包含刚写入的当前用户消息；工具协议事件和失败文本不属于可见对话，不写入消息表。
4. 构造 system 指令与本次当前文档快照，再按数据库顺序追加全部历史消息；当前用户消息不得重复追加。
5. 执行现有最多两轮的流式 tool calling。
6. 普通回答或工具说明轮成功结束后，持久化完整 assistant 可见文本。
7. 工具生成的 change set 使用当前 `chat_session_id`。

失败语义：

- 用户消息在模型调用前持久化；模型失败时保留该未回答消息。
- assistant 消息只在任务成功后写入，不把不完整流式文本加入后续模型上下文。
- 工具成功但第二轮说明失败时，pending change set 和已发出的预览保留；会话详情可恢复该 diff。
- 重试按新的用户消息处理，前端也显示新的用户气泡，避免界面与数据库出现隐藏重复。

## 8. 前端状态与交互

AgentPanel 顶部新增紧凑会话工具栏：会话选择器、新建图标按钮、删除图标按钮。陌生图标带 title；运行中禁用新建、切换和删除。

状态包括：

- `sessions`
- `activeSessionId`
- `messages`
- `activeChanges`
- 会话列表与详情 loading/error

行为：

- 项目或助手变化时关闭旧 SSE、提升作用域 generation、加载会话列表并默认打开最近会话。
- 项目没有历史时显示空白对话；点击新建也进入未持久化空白状态，首次发送由后端创建会话。
- 文档变化只更新下一条请求的 `current_document_id`，不清空会话、消息或 pending diff。
- 会话切换时加载完整消息与 pending diff。
- 首次发送收到 202 后立即记录返回的 `chat_session_id`；任务完成后刷新会话列表以获得自动标题和更新时间。
- SSE 接纳条件改为发起时的 `assistant_id + project_id + chat_session_id` 仍匹配；`document_id` 不再是聊天事件归属条件。
- 存在 pending diff 时前端禁用删除，后端仍以 409 作为最终保护。

## 9. Diff 单一来源与卡片消失修复

聊天产生的 preview 只保存在 AgentPanel 的 `activeChanges`，不再通过 App 复制到 DocumentEditor。DocumentEditor 的 `externalChange` 只服务 selection rewrite。这样同一个 chat change set 只渲染一张卡片。

AgentPanel 点击接受/拒绝后仍通过事件调用 App：

- App 使用 `change.document_version` 调用后端，不要求目标文档当前已打开。
- 若目标 tab 已打开且 dirty，接受前继续提示用户；成功后用返回文档更新 tab。
- 若目标 tab 未打开，仍可完成服务端 apply；无需为了接受 diff 强制切换文档。
- API 成功后 App 调用 `complete(true)`，AgentPanel 按 `change_set_id` 移除卡片。
- API 失败时调用 `complete(false)`，卡片保留并显示错误。
- 重新加载会话只返回服务端仍为 pending 的记录，因此 applied/rejected 卡片不会复活。

## 10. 并发、隔离与错误处理

- 继续使用助手级运行锁；同一助手不能同时执行两个项目聊天任务。
- 新建、读取、删除会话不直接写项目文件，但都必须校验活动项目与助手归属。
- 会话切换或项目切换后到达的旧 HTTP/SSE 响应必须丢弃。
- 运行中禁止切换会话，避免用户误认为流式内容属于另一会话。
- 文档版本冲突仍保留 pending 卡片，并提示用户重新生成或拒绝。
- 数据库迁移必须幂等；旧数据库启动后自动创建新表，现有数据不改写。
- 项目聊天消息不并入普通 Agent Loop 的 FTS 记忆索引；同一项目会话通过精确顺序读取获得完整上下文。

## 11. TDD 与验证

按 RED -> GREEN 分层实施：

1. Memory 测试：会话 CRUD、自动标题、更新时间排序、完整消息顺序、助手/项目/会话隔离、pending 删除冲突和级联清理。
2. Runtime 测试：用户/助手消息持久化、全部历史按顺序进入模型、失败只保留用户消息、tool change set 关联会话、第二轮失败恢复 pending。
3. API 测试：列表、详情、删除、202 返回 session id、跨作用域 404、pending 409。
4. 前端测试：默认最近会话、新建/切换/删除、文档切换保留历史、旧作用域响应丢弃、pending 恢复。
5. 卡片回归：chat preview 不复制到 DocumentEditor；已打开/未打开目标文档均可接受；成功后卡片移除，失败时保留。
6. 先运行 `tests/test_memory_isolation.py`，再运行 Python 全量、前端全量、类型检查和生产构建。

## 12. v1.17 后续演进

本设计在 v1.15 交付时登记的“长会话上下文压缩”已于 v1.17 完成：可见消息仍完整持久化并返回 UI，发给模型的 prompt 则按 token 预算保留最近窗口，将更早历史压缩到 `project_chat_summaries` 并增量复用。

v1.17 还把 pending change set 提升为 App 层单一状态源，同时在 DocumentEditor 内联视图与 AgentPanel 侧栏卡片中呈现。因而本文件 §2、§7 和 §9 中“全部历史直接进入模型”“不实现压缩”“chat preview 只在 AgentPanel 保存”的描述仅适用于 v1.15 交付范围，不再是现行契约。

## 13. 预计改动范围

- `memory/project_chat.py`、`memory/store.py`、助手清理与对应测试。
- `agent/runtime.py`、`agent/project_editing.py` 及 runtime 测试。
- `api/models.py`、`api/main.py`、API 测试。
- `web/src/api/client.ts`、`web/src/types.ts`、`web/src/components/AgentPanel.vue`、`web/src/App.vue` 及前端测试。
- `docs/architecture/phase1-architecture.md` 升至 v1.15；README、AGENTS、导航、新会话提示和待办清单在实现完成后同步。

不改变项目文件 schema、selection rewrite 契约、普通 Agent Loop checkpoint、MCP/Skill 协议或 Scheduler。
