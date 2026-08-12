# 项目 Agent 多会话历史实施计划

> 状态：已完成（Python 156/156、记忆隔离 10/10、前端 41/41，类型检查与生产构建通过；最终复核无 Critical/Important）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为每个助手的每个项目提供可持久化、可切换、可删除的多会话聊天历史，把完整历史传给模型并可靠恢复 pending diff，同时修复接受后重复卡片不消失。

**Architecture:** 新增独立的 `project_chat_sessions/project_chat_messages` 持久化边界，所有 SQL 留在 `memory/` 并由 `MemoryStore` 暴露助手/项目/会话三层隔离接口。项目聊天 API 在入队前创建或校验会话，Runtime 持久化消息并读取完整上下文，AgentPanel 负责会话 UI 和 chat diff 的唯一呈现。

**Tech Stack:** Python 3.13、SQLite、Pydantic v2、FastAPI、OpenAI-compatible streaming tools、Vue 3、TypeScript、Vitest、pytest

**Execution note:** 用户要求在当前工作区直接实施；不得创建新的 docs 目录，不得 commit/push。所有 Python 命令固定使用 `C:\miniconda\envs\writing-agent\python.exe`。

---

### Task 1: 架构 v1.15 与项目聊天持久化

**Files:**
- Create: `memory/project_chat.py`
- Modify: `memory/store.py`
- Modify: `memory/projects.py`
- Modify: `docs/architecture/phase1-architecture.md`
- Test: `tests/test_project_chat_history.py`
- Test: `tests/test_memory_isolation.py`

- [ ] **Step 1: 先把架构单一事实来源升至 v1.15**

在任何生产代码前，将已确认设计中的表、MemoryStore 方法、API、完整上下文、pending 删除冲突和 diff 单一来源写入 `phase1-architecture.md`，状态标为实施中。

- [ ] **Step 2: 写持久化成功路径与隔离 RED**

创建 `tests/test_project_chat_history.py`，先覆盖：

```python
session = store.create_project_chat_session("writer-a", project.project_id)
store.add_project_chat_message(
    "writer-a", project.project_id, session.chat_session_id, "user", "第一条需求"
)
store.add_project_chat_message(
    "writer-a", project.project_id, session.chat_session_id, "assistant", "第一条回答"
)
assert [item.content for item in store.list_project_chat_messages(
    "writer-a", project.project_id, session.chat_session_id
)] == ["第一条需求", "第一条回答"]
assert store.list_project_chat_sessions("writer-a", project.project_id)[0].title == "第一条需求"
```

再断言相同 `chat_session_id` 不能由其他助手或项目读取。`tests/test_memory_isolation.py` 增加项目聊天消息跨助手不可见的红线。

- [ ] **Step 3: 运行并确认 RED**

```powershell
C:\miniconda\envs\writing-agent\python.exe -m pytest tests\test_project_chat_history.py tests\test_memory_isolation.py -q -p no:cacheprovider --basetemp D:\test_agent\pytest-temp-writing-agent-chat-memory-red
```

预期因 `create_project_chat_session` 等接口不存在而失败。

- [ ] **Step 4: 实现专用 memory 模块与 Store 包装**

`memory/project_chat.py` 定义不可变记录：

```python
@dataclass(frozen=True)
class ProjectChatSessionRecord:
    chat_session_id: str
    assistant_id: str
    project_id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int = 0

@dataclass(frozen=True)
class ProjectChatMessageRecord:
    message_id: int
    assistant_id: str
    project_id: str
    chat_session_id: str
    role: str
    content: str
    created_at: str
```

实现幂等 DDL、项目归属验证、创建/列表/读取/写消息/删除。所有查询同时携带 `assistant_id/project_id/chat_session_id`；第一条用户消息在同一事务中更新标题与 `updated_at`。

`MemoryStore` 暴露设计文档列出的七个入口，并在初始化时建表。`projects.purge_project` 与 `MemoryStore.purge_assistant` 级联清理项目聊天表。

- [ ] **Step 5: 补 pending 恢复与删除冲突 RED/GREEN**

创建 chat change set 后断言：

```python
pending = store.list_pending_chat_changes(
    "writer-a", project.project_id, session.chat_session_id
)
assert [item.change_set_id for item in pending] == [change.change_set_id]
with pytest.raises(ResourceConflictError, match="待处理修改"):
    store.delete_project_chat_session(
        "writer-a", project.project_id, session.chat_session_id
    )
```

apply/reject 后删除会话应成功，文档已应用内容保持不变；项目 purge 与助手 purge 后新表无残留。

- [ ] **Step 6: 验证 Task 1 GREEN**

运行 Task 1 两个测试文件，预期全部通过。

---

### Task 2: Runtime 持久化与完整模型上下文

**Files:**
- Modify: `agent/runtime.py`
- Modify: `agent/project_editing.py`
- Test: `tests/test_runtime_project_editing.py`

- [ ] **Step 1: 写完整历史进入模型的 RED**

预建同会话历史，再发当前消息，断言首轮请求的可见角色/内容顺序：

```python
assert [(item["role"], item["content"]) for item in runtime.llm.calls[0]["messages"][1:]] == [
    ("user", "上一条问题"),
    ("assistant", "上一条回答"),
    ("user", "当前问题"),
]
```

同时断言当前消息只出现一次、当前文档快照只在 system 指令中出现。

- [ ] **Step 2: 运行单测确认 RED**

```powershell
C:\miniconda\envs\writing-agent\python.exe -m pytest tests\test_runtime_project_editing.py -k "history or persists" -q -p no:cacheprovider --basetemp D:\test_agent\pytest-temp-writing-agent-chat-runtime-red
```

- [ ] **Step 3: 修改 Runtime 会话参数与消息流**

`chat_project` 新签名：

```python
async def chat_project(
    self,
    assistant_id: str,
    project_id: str,
    message: str,
    *,
    chat_session_id: str,
    current_document_id: str | None = None,
) -> ProjectChatResult:
```

持锁后重新验证项目/会话/文档，先保存 user 消息，再读取完整历史。system 消息承载 persona、editing 指令、工具规则和本次文档快照；历史作为标准 `user/assistant` 消息按数据库顺序附加。任务成功后保存完整 assistant reply；工具 change set 的 `ToolContext.session_id` 使用 `chat_session_id`。

- [ ] **Step 4: 写失败语义与 pending 关联测试**

覆盖：首轮失败只保留 user；成功保存 assistant；第二轮失败不保存 assistant 但 pending change set 的 `session_id` 正确且可恢复；空白文档首稿仍可用。

- [ ] **Step 5: 验证 Task 2 GREEN**

运行 `tests/test_runtime_project_editing.py` 全文件，预期全部通过。

---

### Task 3: 会话 API 与 202 预校验

**Files:**
- Modify: `api/models.py`
- Modify: `api/main.py`
- Test: `tests/test_api_projects.py`

- [ ] **Step 1: 写会话 API RED**

覆盖：列表按更新时间倒序、详情返回 messages/pending_changes、删除成功、pending 删除返回 409、跨助手/跨项目返回 404。

```python
response = client.get(
    f"/api/projects/{project_id}/agent/sessions/{session_id}",
    params={"assistant_id": "default"},
)
assert response.json()["messages"][0]["content"] == "第一条需求"
assert response.json()["pending_changes"][0]["change_set_id"] == change_id
```

- [ ] **Step 2: 写发送接口返回会话 id 的 RED**

请求不带 `chat_session_id` 时断言 202 响应同时含 `task_id/chat_session_id`；带已有 id 时复用。不存在会话、跨项目会话和无效 `current_document_id` 必须在入队前返回 404，TaskBroker 不产生记录。

- [ ] **Step 3: 运行 API 测试确认 RED**

```powershell
C:\miniconda\envs\writing-agent\python.exe -m pytest tests\test_api_projects.py -q -p no:cacheprovider --basetemp D:\test_agent\pytest-temp-writing-agent-chat-api-red
```

- [ ] **Step 4: 实现 API 与响应映射**

`ProjectChatRequest` 新增：

```python
chat_session_id: str | None = None
```

新增三个 GET/DELETE 路由。发送路由先验证助手锁、项目、可选文档和已有会话；未传 id 时同步创建会话，再把 id 传给 Runtime，并返回：

```python
{"task_id": broker.start(body.assistant_id, operation), "chat_session_id": chat_session_id}
```

- [ ] **Step 5: 验证 Task 3 GREEN**

运行 API 项目测试，预期全部通过。

---

### Task 4: AgentPanel 会话加载、切换与完整历史

**Files:**
- Modify: `web/src/types.ts`
- Modify: `web/src/api/client.ts`
- Modify: `web/src/components/AgentPanel.vue`
- Modify: `web/src/components/AgentPanel.test.ts`

- [ ] **Step 1: 写最近会话恢复与文档切换保留 RED**

Mock `listProjectChatSessions/getProjectChatSession`，挂载后断言自动加载第一项。随后只改变 `documentId`：

```typescript
await wrapper.setProps({ documentId: 'document-2' })
expect(wrapper.get('.message.user').text()).toContain('历史问题')
expect(apiMocks.getProjectChatSession).toHaveBeenCalledTimes(1)
```

- [ ] **Step 2: 写新建、切换、删除与 pending 恢复 RED**

覆盖新建后进入空白状态、首次发送接纳响应 session id、选择器切换加载详情、pending diff 恢复、pending 时删除按钮禁用、删除成功后回到最近会话。

- [ ] **Step 3: 写 SSE 会话作用域 RED**

文档变化后原会话 token 仍追加；会话或项目变化后旧 token 被丢弃。运行中选择器和按钮禁用。

- [ ] **Step 4: 实现类型、客户端与 AgentPanel 状态机**

新增：

```typescript
export interface ProjectChatSession { chat_session_id: string; title: string; created_at: string; updated_at: string; message_count: number }
export interface ProjectChatMessage { message_id: number; role: 'user' | 'assistant'; content: string; created_at: string }
export interface ProjectChatSessionDetail { session: ProjectChatSession; messages: ProjectChatMessage[]; pending_changes: ChangePreview[] }
```

客户端增加 list/get/delete，会话发送方法接收可空 session id 并返回两种 id。AgentPanel 的项目 watch 负责加载会话；document watch 不清空。顶部使用原生 select、Plus 与 Trash2 图标按钮。

- [ ] **Step 5: 验证 AgentPanel GREEN**

```powershell
Set-Location D:\test_agent\writing-agent\web
npm test -- src/components/AgentPanel.test.ts
```

---

### Task 5: Diff 单一来源与接受后卡片移除

**Files:**
- Modify: `web/src/components/AgentPanel.vue`
- Modify: `web/src/App.vue`
- Modify: `web/src/App.test.ts`
- Test: `web/src/components/AgentPanel.test.ts`

- [ ] **Step 1: 写未打开目标文档也可接受的 RED**

直接调用 App 的 `applyAgentChange`，workspace 中没有目标 tab，断言仍调用：

```typescript
expect(apiMocks.applyChange).toHaveBeenCalledWith(
  'default', change.project_id, change.change_set_id, change.document_version,
)
expect(complete).toHaveBeenCalledWith(true)
```

- [ ] **Step 2: 写 chat preview 不复制到编辑器的 RED**

AgentPanel 收到 `change_preview` 后只出现一个 `.change-diff`；App 的 `externalChange` 不被 chat preview 设置。selection rewrite 的 `DocumentEditor @preview` 行为保持。

- [ ] **Step 3: 实现 App apply 与单一来源**

移除 AgentPanel 的 `preview` emit 和 App 上对应监听。`applyAgentChange` 使用 `change.document_version` 请求；目标 tab 存在时处理 dirty 确认并用响应刷新，目标 tab 不存在时仍 apply。成功调用 `complete(true)`，失败调用 `complete(false)`。

- [ ] **Step 4: 验证卡片行为 GREEN**

运行 `App.test.ts` 与 `AgentPanel.test.ts`，断言 apply/reject 成功移除、失败保留、重新加载已处理会话不返回卡片。

---

### Task 6: 待办、文档同步与全量回归

**Files:**
- Create: `docs/guides/backlog.md`
- Modify: `AGENTS.md`
- Modify: `README.md`
- Modify: `docs/README.md`
- Modify: `docs/guides/new-session-prompt.md`
- Modify: `docs/architecture/phase1-architecture.md`
- Modify: `docs/architecture/project-chat-history-design.md`

- [ ] **Step 1: 先跑记忆隔离红线**

```powershell
C:\miniconda\envs\writing-agent\python.exe -m pytest tests\test_memory_isolation.py -q -p no:cacheprovider --basetemp D:\test_agent\pytest-temp-writing-agent-chat-isolation
```

预期全部通过。

- [ ] **Step 2: 跑 Python 全量**

```powershell
C:\miniconda\envs\writing-agent\python.exe -m pytest tests -q -p no:cacheprovider --basetemp D:\test_agent\pytest-temp-writing-agent-chat-full
```

- [ ] **Step 3: 跑前端全量、类型检查和构建**

```powershell
Set-Location D:\test_agent\writing-agent\web
npm test
npm run typecheck
npm run build
```

- [ ] **Step 4: 登记上下文压缩待办并同步基线**

`docs/guides/backlog.md` 明确记录：未来按 token 预算压缩旧消息，保留最近原文与 pending diff 上下文；本阶段传入全部消息，不静默截断。将 v1.15 状态改为完成，并按实际输出更新测试数量，不猜测。

- [ ] **Step 5: 最终检查并重启本地服务**

```powershell
git diff --check
git status --short --branch
Get-ChildItem docs -Directory
```

确认 docs 仍只有 `architecture/guides/history/reviews`，无 `.env`、数据库、日志或构建产物进入改动。重启本会话启动的 `127.0.0.1:8001` 服务并请求主页与 API；保持全部改动未提交，交用户验收。
