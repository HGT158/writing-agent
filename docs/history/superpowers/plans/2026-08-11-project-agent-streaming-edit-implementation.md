# 项目 Agent 流式编辑实施计划

> 状态：已完成（Python 143/143、记忆隔离 9/9、前端 32/32，类型检查与生产构建通过；最终复核无 Critical/Important）



**Goal:** 让网页侧边栏项目 Agent 通过真实 SSE 文本增量回答，并以项目作用域工具生成可接受/拒绝的 change set，而不直接写正文。

**Architecture:** 保留现有 `POST /agent/messages -> TaskBroker -> SSE` 和 change set/apply 链路。项目聊天第一轮使用 OpenAI-compatible 流式 tool calling；`propose_project_edits` 只创建 pending change set，成功后进行至多一个无工具说明轮次。前端把同一任务的 token 追加到一个气泡，并把工具事件呈现为紧凑状态。

**Tech Stack:** Python 3.13、OpenAI Python SDK 2.x、Pydantic v2、FastAPI/SSE、Vue 3、TypeScript、Vitest、pytest

**Execution note:** 项目没有可用的 `executing-plans` 技能，且用户要求当前会话直接实施，因此按本计划内联执行。不得创建额外文档目录，不得 commit/push。

---

### Task 1: 项目作用域编辑提案工具

**Files:**
- Modify: `agent/project_editing.py`
- Modify: `agent/tools.py`
- Test: `tests/test_tool_registry.py`

- [x] **Step 1: 写工具成功路径的针对性测试**

在 `tests/test_tool_registry.py` 创建项目与正文，调用尚不存在的 `make_project_edit_tool`，断言返回一个 pending change set 且正文未变化：

```python
args = {
    "changes": [{
        "document_id": document.document_id,
        "old_text": "第一段原文。",
        "new_text": "首段精简。",
        "document_version": document.version,
    }],
}
result = json.loads(asyncio.run(spec.call(args, ctx)))
change = store.get_change_set("default", project.project_id, result["change_set_ids"][0])
assert change.original_text == "第一段原文。"
assert change.replacement_text == "首段精简。"
assert store.get_document("default", project.project_id, document.document_id).content == original
```

- [x] **Step 2: 运行测试**

```powershell
C:\miniconda\envs\writing-agent\python.exe -m pytest tests\test_tool_registry.py::test_project_edit_tool_creates_pending_change_without_writing_document -q -p no:cacheprovider --basetemp D:\test_agent\pytest-temp-writing-agent
```

预期：因 `make_project_edit_tool` 尚不存在而失败。

- [x] **Step 3: 实现输入模型与工具**

在 `agent/project_editing.py` 定义强类型输入：

```python
class ProjectEditChange(BaseModel):
    document_id: str
    old_text: str = Field(min_length=1)
    new_text: str
    document_version: int = Field(ge=1)


class ProjectEditBatch(BaseModel):
    changes: list[ProjectEditChange] = Field(min_length=1)
```

在 `agent/tools.py` 增加工厂。闭包绑定 `project_id`，先检查同文档重复、版本、正文和唯一精确匹配，再一次性调用 `MemoryStore.create_change_sets`：

```python
def make_project_edit_tool(store: MemoryStore, project_id: str) -> ToolSpec:
    async def propose(args: dict[str, Any], ctx: ToolContext) -> str:
        batch = ProjectEditBatch.model_validate(args)
        seen: set[str] = set()
        drafts: list[dict[str, object]] = []
        for item in batch.changes:
            if item.document_id in seen:
                raise ValueError("同一次编辑调用中每个文档只能出现一次")
            seen.add(item.document_id)
            document = store.get_document(ctx.assistant_id, project_id, item.document_id)
            if document.version != item.document_version:
                raise ResourceConflictError("版本冲突")
            content = document.content or ""
            start = content.find(item.old_text)
            if start < 0:
                raise ResourceConflictError("旧文本不存在")
            if content.find(item.old_text, start + 1) >= 0:
                raise ResourceConflictError("旧文本匹配多处，请提供更多上下文")
            drafts.append({
                "document_id": item.document_id,
                "start": start,
                "end": start + len(item.old_text),
                "original_text": item.old_text,
                "replacement_text": item.new_text,
                "base_version": item.document_version,
            })
        changes = store.create_change_sets(
            ctx.assistant_id, project_id, drafts, source="chat", session_id=ctx.session_id,
        )
        return json.dumps({"change_set_ids": [item.change_set_id for item in changes]})

    return ToolSpec(
        name="propose_project_edits",
        description="为项目文档提出精确修改建议；必须用于改写、增删或替换正文，工具不会直接写文件。",
        args_schema=ProjectEditBatch.model_json_schema(),
        handler=propose,
        idempotent=False,
        captures_source=False,
    )
```

- [x] **Step 4: 验证实现**

运行 Task 1 的单测，预期 PASS。

- [x] **Step 5: 补充失败边界测试并完成实现**

新增并逐个运行四个测试：`test_project_edit_tool_rejects_missing_old_text_atomically` 传入正文不存在的 `old_text`；`test_project_edit_tool_rejects_ambiguous_old_text_atomically` 使用包含两次“重复句。”的正文；`test_project_edit_tool_rejects_duplicate_document_changes` 在同一 `changes` 数组重复同一 `document_id`；`test_project_edit_tool_rejects_stale_document_version` 传入 `document.version - 1`。每个测试都用 `pytest.raises` 断言对应错误，并断言 `store.get_document("default", project.project_id, document.document_id).content` 等于调用前正文、该项目 pending change set 数量为零。完成实现后运行测试并确认通过。

---

### Task 2: OpenAI-compatible 文本与工具参数流适配

**Files:**
- Modify: `agent/llm.py`
- Test: `tests/test_runtime_project_editing.py`

- [x] **Step 1: 写多文本 delta 的针对性测试**

构造异步 fake stream，依次产生 `你`、`好` 两个 `delta.content`，调用期望的新接口并断言回调和最终文本：

```python
turn = asyncio.run(stream_chat_turn(
    fake_llm, "fake", messages,
    on_text=chunks.append,
))
assert chunks == ["你", "好"]
assert turn.text == "你好"
assert turn.tool_calls == []
```

- [x] **Step 2: 运行测试**

```powershell
C:\miniconda\envs\writing-agent\python.exe -m pytest tests\test_runtime_project_editing.py::test_stream_chat_turn_forwards_text_deltas -q -p no:cacheprovider --basetemp D:\test_agent\pytest-temp-writing-agent
```

预期：`stream_chat_turn` 尚不存在。

- [x] **Step 3: 实现流式轮次模型和累积器**

在 `agent/llm.py` 增加：

```python
@dataclass(frozen=True)
class StreamedToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class StreamedTurn:
    text: str
    tool_calls: list[StreamedToolCall]


async def stream_chat_turn(
    llm: AsyncOpenAI,
    model: str,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    on_text: Callable[[str], None] | None = None,
    max_tool_argument_chars: int = 1024 * 1024,
) -> StreamedTurn:
    kwargs = {"model": model, "messages": messages, "temperature": 0.3, "stream": True}
    if tools is not None:
        kwargs["tools"] = tools
        kwargs["parallel_tool_calls"] = False
    stream = await llm.chat.completions.create(**kwargs)
    text_parts: list[str] = []
    calls: dict[int, dict[str, str]] = {}
    async for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta.content:
            text_parts.append(delta.content)
            if on_text is not None:
                on_text(delta.content)
        for item in delta.tool_calls or []:
            current = calls.setdefault(item.index, {"id": "", "name": "", "arguments": ""})
            if item.id:
                current["id"] = item.id
            if item.function and item.function.name:
                current["name"] = item.function.name
            if item.function and item.function.arguments:
                current["arguments"] += item.function.arguments
                if len(current["arguments"]) > max_tool_argument_chars:
                    raise RuntimeError("工具参数超过 1 MiB 上限")
    tool_calls = [StreamedToolCall(**calls[index]) for index in sorted(calls)]
    if any(not item.id or not item.name for item in tool_calls):
        raise RuntimeError("工具调用流不完整")
    return StreamedTurn(text="".join(text_parts), tool_calls=tool_calls)
```

Provider 在首轮拒绝 `tools` 或流式参数时，将 `BadRequestError` 转为明确的 `RuntimeError("当前模型服务不支持项目 Agent 流式编辑工具")`；网络、鉴权、限流错误保持原异常。

- [x] **Step 4: 验证文本流**

运行 Task 2 单测，预期 PASS。

- [x] **Step 5: 为 tool-call 分片与上限逐个补 测试与实现**

新增三个测试并逐个观察测试结果：`test_stream_chat_turn_accumulates_tool_argument_deltas` 让 index 0 的参数分成 `{"changes":[` 与 `]}`，断言得到一个 id/name 完整且 arguments 为两段拼接结果的 `StreamedToolCall`；`test_stream_chat_turn_rejects_tool_arguments_over_limit` 把测试上限设为 8 并输入 9 个参数字符，断言错误为“工具参数超过”；`test_stream_chat_turn_rejects_incomplete_tool_call` 只发送 name 不发送 id，断言错误为“工具调用流不完整”。逐个补最小实现并确认实现。

---

### Task 3: 有界项目聊天工具循环

**Files:**
- Modify: `agent/runtime.py`
- Modify: `agent/project_editing.py`
- Test: `tests/test_runtime_project_editing.py`

- [x] **Step 1: 写普通聊天真实流式的针对性测试**

FakeLLM 返回 `我建议`、`先精简。` 两个 chunk：

```python
result = asyncio.run(runtime.chat_project(
    "default", project.project_id, "有什么问题？",
    current_document_id=document.document_id,
))
assert result.reply == "我建议先精简。"
assert [event["data"]["text"] for event in events if event["type"] == "token"] == [
    "我建议", "先精简。",
]
```

- [x] **Step 2: 运行测试**

```powershell
C:\miniconda\envs\writing-agent\python.exe -m pytest tests\test_runtime_project_editing.py::test_project_chat_streams_plain_reply_deltas -q -p no:cacheprovider --basetemp D:\test_agent\pytest-temp-writing-agent
```


- [x] **Step 3: 将普通回答切到 `stream_chat_turn`**

在 `chat_project` 中用回调同步发事件：

```python
visible: list[str] = []

def emit_text(delta: str) -> None:
    visible.append(delta)
    self.bus.emit("token", text=delta)

first = await stream_chat_turn(
    self.llm, self.settings.model_name, messages,
    tools=[tool_to_openai_schema(edit_tool)], on_text=emit_text,
)
```

第一轮没有工具调用时返回 `ProjectChatResult(reply="".join(visible), changes=[])`。

- [x] **Step 4: 验证普通聊天**

运行单测，预期 PASS。

- [x] **Step 5: 写编辑工具两轮链路的针对性测试**

首轮 fake stream 返回一个 `propose_project_edits` tool call 的分片参数，第二轮返回 `已生成`、`修改建议。`。断言：

```python
assert result.reply == "已生成修改建议。"
assert len(result.changes) == 1
assert event_types.count("tool_call") == 1
assert event_types.count("tool_result") == 1
assert event_types.count("change_preview") == 1
assert runtime.store.get_document("default", project.project_id, document.document_id).content == original
assert runtime.llm.calls[1].get("tools") is None
```

- [x] **Step 6: 实现单工具执行和第二轮说明**

验证首轮最多一个、名称必须为 `propose_project_edits`，解析完整 JSON 后才发 `tool_call` 并执行：

```python
if len(first.tool_calls) != 1 or first.tool_calls[0].name != edit_tool.name:
    raise RuntimeError("项目聊天每轮只允许一个编辑提案工具调用")
call = first.tool_calls[0]
args = json.loads(call.arguments)
self.bus.emit("tool_call", tool=call.name, args={"changes": len(args.get("changes", []))})
try:
    output = await edit_tool.call(args, ctx)
except Exception as exc:
    self.bus.emit("tool_result", tool=call.name, ok=False, error=str(exc))
    raise
self.bus.emit("tool_result", tool=call.name, ok=True, summary="修改建议已生成")
ids = json.loads(output)["change_set_ids"]
changes = [self.store.get_change_set(assistant_id, project_id, item) for item in ids]
```

逐条发 `change_preview`，随后把标准 assistant tool-call 和 tool result 消息加入上下文，第二轮不传 `tools`。若第一轮已有文本，在第二轮首 token 前发 `\n\n`。

- [x] **Step 7: 验证编辑链路**

运行工具链路测试，预期 PASS。

- [x] **Step 8: 补协议和失败恢复测试**

逐个 测试与实现 增加四个测试：`test_project_chat_rejects_multiple_tool_calls_without_changes` 输入两个不同 call id，断言抛协议错误且 pending 数量为零；`test_project_chat_emits_failed_tool_result_for_invalid_edit` 输入不存在的旧文本，断言最后一个 `tool_result.ok` 为 false 且没有预览；`test_project_chat_keeps_pending_change_when_followup_stream_fails` 让首轮工具成功、第二轮抛 `RuntimeError("followup down")`，断言 pending change set 与 `change_preview` 均保留；`test_project_chat_releases_lock_when_stream_fails` 让首轮直接抛流异常，断言 `store.is_locked("default")` 为 false。

---

### Task 4: 前端合并 token 并呈现工具状态

**Files:**
- Modify: `web/src/components/AgentPanel.vue`
- Modify: `web/src/components/AgentPanel.test.ts`
- Modify: `web/src/styles.css`

- [x] **Step 1: 写多个 token 只形成一个气泡的针对性测试**

```typescript
await callback(taskEvent('token', { text: '正在' }))
await callback(taskEvent('token', { text: '处理。' }))
await nextTick()
expect(wrapper.findAll('.message.assistant')).toHaveLength(1)
expect(wrapper.get('.message.assistant p').text()).toBe('正在处理。')
```

- [x] **Step 2: 运行测试**

```powershell
Set-Location D:\test_agent\writing-agent\web
npm test -- src/components/AgentPanel.test.ts -t "appends streamed tokens to one assistant message"
```

预期：当前组件为每个 token push 一个消息，气泡数量为 2。

- [x] **Step 3: 实现单任务消息累积**

新增当前回复索引，在每次 `send` 开始时重置：

```typescript
let assistantMessageIndex: number | null = null

function appendAssistantDelta(text: string) {
  if (!text) return
  if (assistantMessageIndex === null) {
    messages.value.push({ role: 'assistant', content: text })
    assistantMessageIndex = messages.value.length - 1
    return
  }
  messages.value[assistantMessageIndex].content += text
}
```

`token` 分支调用 `appendAssistantDelta`；发送新指令和作用域切换时把索引置空。

- [x] **Step 4: 验证 token 合并**

运行指定 Vitest，预期 PASS。

- [x] **Step 5: 写工具状态的针对性测试**

```typescript
await callback(taskEvent('tool_call', { tool: 'propose_project_edits' }))
expect(wrapper.get('.tool-status').text()).toContain('正在准备修改')
await callback(taskEvent('tool_result', { tool: 'propose_project_edits', ok: true }))
expect(wrapper.get('.tool-status').text()).toContain('修改建议已生成')
```

- [x] **Step 6: 实现工具状态与样式**

新增 `toolStatus`，只响应 `propose_project_edits`：

```typescript
if (event.type === 'tool_call' && event.data.tool === 'propose_project_edits') {
  toolStatus.value = 'Agent 正在准备修改'
}
if (event.type === 'tool_result' && event.data.tool === 'propose_project_edits') {
  toolStatus.value = event.data.ok ? '修改建议已生成' : String(event.data.error || '修改建议生成失败')
}
```

模板在消息区渲染 `<p v-if="toolStatus" class="tool-status">{{ toolStatus }}</p>`；样式使用现有中性色与 `--accent`，不新增卡片嵌套。

- [x] **Step 7: 验证工具状态并回归组件测试**

```powershell
Set-Location D:\test_agent\writing-agent\web
npm test -- src/components/AgentPanel.test.ts
```

预期：AgentPanel 全部测试通过。

---

### Task 5: API 事件回放、全量回归与文档收口

**Files:**
- Modify: `tests/test_api_projects.py`
- Modify: `tests/test_runtime_project_editing.py`
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `docs/architecture/phase1-architecture.md`
- Modify: `docs/architecture/project-agent-streaming-edit-design.md`

- [x] **Step 1: 写 API SSE 事件序列的失败断言**

更新项目聊天 API 测试的 FakeLLM 为异步流，断言响应流包含：

```python
assert stream.text.count('"type": "token"') >= 2
assert '"type": "tool_call"' in stream.text
assert '"type": "tool_result"' in stream.text
assert '"type": "change_preview"' in stream.text
assert '"type": "task_done"' in stream.text
```

- [x] **Step 2: 运行测试并完成 FakeLLM/接口适配**

```powershell
C:\miniconda\envs\writing-agent\python.exe -m pytest tests\test_api_projects.py::test_project_agent_chat_returns_streamed_reply_and_change_preview -q -p no:cacheprovider --basetemp D:\test_agent\pytest-temp-writing-agent
```

更新 fake/事件数量所需的测试夹具和实现，不改变 API 路径或响应 schema，并确认测试通过。

- [x] **Step 3: 先跑记忆隔离红线**

```powershell
C:\miniconda\envs\writing-agent\python.exe -m pytest tests\test_memory_isolation.py -q -p no:cacheprovider --basetemp D:\test_agent\pytest-temp-writing-agent
```

预期 9/9 PASS。

- [x] **Step 4: 跑 Python 全量**

```powershell
C:\miniconda\envs\writing-agent\python.exe -m pytest tests -q -p no:cacheprovider --basetemp D:\test_agent\pytest-temp-writing-agent
```

预期全部通过，无 warning/error。

- [x] **Step 5: 跑前端全量、类型检查和构建**

```powershell
Set-Location D:\test_agent\writing-agent\web
npm test
npm run typecheck
npm run build
```

预期全部通过。

- [x] **Step 6: 更新实际基线与状态**

把 README、AGENTS 和架构文档中的 Python/隔离/前端测试数量更新为本次实际输出；把 v1.13 状态改为“实现完成，文档与代码已同步”，专项设计状态改为“已实现”。不得猜测测试数量。

- [x] **Step 7: 最终差异检查**

```powershell
git diff --check
git status --short --branch
git diff --stat
```

确认 `.env`、`data/`、数据库、日志、依赖、构建产物未进入索引。保持所有改动未提交，等待用户审查。
