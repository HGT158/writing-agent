# 阶段 5 代码审查报告

> 审查对象：`writing-agent/` 阶段 4 提交（`8eef123`）之后的全部未提交改动，即 v1.13 项目 Agent 流式编辑、v1.14 空白文档首稿生成、v1.15 项目 Agent 多会话历史三个增量：`agent/`（llm.py / project_editing.py / runtime.py / tools.py）、`memory/`（store.py / projects.py / 新增 project_chat.py）、`api/`（main.py / models.py）、`web/src/`（AgentPanel.vue / App.vue / client.ts / types.ts / styles.css 及测试）、新增测试 `tests/test_project_chat_history.py`，以及配套文档（架构 v1.15、两份设计文档、backlog、README/AGENTS/导航/新会话提示）
> 审查日期：2026-08-12
> 审查方式：逐文件走读全部 diff（24 个改动文件 + 2 个新增源码文件）+ 关键路径动态复现（独立临时 data 目录，未改动任何项目代码）+ `pytest tests/`、记忆隔离红线、`vitest run` / `vue-tsc -b` / `npm run build` 全量实测
> 环境：Windows 11 / Python（conda `writing-agent`）/ Node + vue-tsc / 与 AGENTS.md 声明环境一致

---

## 总体评价

先说做得好的部分：

- **设计与实现严格对齐**：两份设计文档（流式编辑、多会话历史）的目标与失败语义在代码中逐条可对应；架构文档同步升至 v1.15，变更说明、§4.7/§5.2/§5.4/§5.7/§5.9/§6.2/§9 契约、事件样例和风险表全部更新，README/AGENTS/导航/backlog 一并同步，未发现"只改文档不改行为"或"只改行为不升文档"。
- **编辑链路安全边界保持完好**：`propose_project_edits`（`agent/tools.py:129-192`）只创建 pending change set，绝不写正文；闭包绑定服务端已校验的 `project_id`，`assistant_id` 来自 `ToolContext` 不可被模型伪造；空 `old_text` 仅当正文为空时放行并固定为 `[0, 0)` 插入（v1.14 修复），非空文档空旧文本整批拒绝；`create_change_sets`（`memory/projects.py:1035-1093`）单事务全量校验、任一非法整批回滚，与阶段 4 定稿一致。
- **三层隔离执行到位**：`memory/project_chat.py` 所有查询/写入均同时携带 `assistant_id + project_id + chat_session_id`，全部参数化绑定；`_require_project` 强制 `archived_at IS NULL`，归档项目聊天 API 统一 404；`list_pending_chat_changes` 只匹配 `source='chat'` 且 `status='pending'`，不会误收选区改写建议。`tests/test_memory_isolation.py` 新增跨助手聊天隔离用例，红线 10/10 常绿。
- **会话生命周期保护充分**：删除会话在同一事务内先查 pending chat change set（409），再删消息、会话与已处理 chat change set 元数据，已应用正文不回滚（`project_chat.py:222-258`）；`MemoryStore.delete_project_chat_session` 先取助手级运行锁，运行中删除映射 409（`store.py:187-196`）；项目 purge 与助手 purge 均级联清理两张新表且有测试固化。
- **流式实现有界且失败语义清晰**：`stream_chat_turn`（`agent/llm.py:54-110`）对 tool-call 参数按 UTF-8 字节限流 1 MiB、拒绝不完整工具流、对"不支持流式 tools"的 BadRequest 做启发式识别并转可读错误；runtime 两轮有界循环（`agent/runtime.py:208-388`）禁止递归工具调用，工具失败发 `tool_result(ok=false)` 再终态失败，第二轮失败保留已创建的 pending 建议（有测试 `test_project_chat_keeps_pending_change_when_followup_stream_fails`）。
- **接受链路统一且修复了阶段 4 的静态 tab 依赖**：`App.applyAgentChange` 改用 `change.document_version` 提交（`App.vue:136-141`），目标文档未打开也可接受；chat preview 不再复制到 DocumentEditor（`@preview` 事件已从 AgentPanel 移除），同一 chat change set 只渲染一张卡片；DocumentEditor 已有 `tab.content` watcher 同步编辑器，阶段 4 P0-2 修复仍在位。
- **API 前置校验完整**：`POST /agent/messages` 在入队前完成消息非空、助手与运行锁、项目树、当前文档、会话归属/创建全部校验（`api/main.py:275-312`），符合"不得先入队再异步暴露归属错误"；会话列表/详情/删除三个新端点均以 `assistant_id` 强制过滤。
- **阶段 4 遗留四项全部闭环**（详见下文专项核对）：写意图 TTL + 进程启动时间防 PID 复用、`con .txt` 保留名、任务端点助手预检、崩溃残骸清扫均已在位并有测试。
- **回归无退化且基线真实**：本次实测 Python **156/156**（22.41s）、记忆隔离 **10/10**、前端 **41/41**、`vue-tsc -b` 干净、生产构建成功，与 AGENTS.md/README/架构文档声明完全一致。

本阶段发现 **0 个 P0、0 个 P1、2 个 P2、9 个 P3**。没有数据损坏、越权或安全问题；两类新表、批量 change set、运行锁与 SSE 作用域的关键路径均经得起走读与动态复现。问题集中在任务失败时的残留清理（空会话、前端气泡）与少量健壮性/体验细节。

测试现状（本次实测）：

```
C:\miniconda\envs\writing-agent\python.exe -m pytest tests\test_memory_isolation.py -q  → 10 passed
C:\miniconda\envs\writing-agent\python.exe -m pytest tests -q                            → 156 passed
npm test                                                                                  → 41 passed (8 files)
npm run typecheck                                                                         → 干净
npm run build                                                                             → 成功
```

---

## P0 — 阻断验收

未发现。

## P1 — 应修复

未发现。以下高风险路径已逐一核查确认安全，列出以备追溯：

- **批量 change set 原子性**：工具侧先读文档计算偏移，`create_change_sets` 在 `BEGIN IMMEDIATE` 内再次校验版本与原文快照，任一失败整批回滚，不存在部分 pending（`tools.py:129-175`、`projects.py:1052-1089`，测试 `test_project_chat_rolls_back_all_changes_when_one_change_is_invalid`）。
- **非可编辑文档**：`_load_document` 对非可编辑文档返回 `content=None`（`projects.py:414`），工具侧空内容只会命中"旧文本不存在"，`create_change_sets` 对 `content is None` 直接拒绝，无法为非可编辑文档造出不可应用的 pending。
- **会话删除与 apply 并发**：apply 全程 change set 保持 pending 直至 finalize，删除事务见到 pending 即 409；SQLite 事务串行化关闭了窗口。
- **跨会话/跨助手泄漏**：所有聊天查询三键过滤，详情端点 pending_changes 按 `session_id` 精确匹配；隔离测试固化。
- **XSS**：聊天消息与会话标题全部文本插值渲染，未新增任何 `v-html`。

---

## P2 — 建议修复

### 1. API 层先建会话、任务后执行：任务失败留下永久孤儿空会话

**位置**：`api/main.py:286-294`（`chat_session_id` 为空时同步 `create_project_chat_session` 后才 `broker.start`）；`agent/runtime.py:218-234`（任务内先查 API Key、取运行锁、再校验项目/会话/文档，最后才写用户消息）

`chat_session_id=None` 的首次发送会在入队前就把会话落库，但任务在写入第一条用户消息之前存在多个失败点：未配置 `OPENAI_API_KEY`（`runtime.py:218`）、并发请求间的运行锁竞争（`validate_task_submission` 的 check-then-start 窗口）、运行期文档校验失败等。任一失败都会留下 0 条消息的"新对话"会话，且没有任何自动清理——它永久占据会话列表，只能手工删除。

**复现证据**（独立临时库，模拟"建会话后任务失败"）：

```
孤儿会话进入列表: [('新对话', 0)]
删除后: []
```

最典型的触发场景是 `.env` 未配置 Key：此时每一次"新对话"发送都失败并各留下一个空会话，列表会快速污染。

**修复建议**（三选一）：会话创建下沉到 runtime 取锁并校验成功之后，`chat_session_id` 通过 202 响应之外的通道（如首个 SSE 事件或 task_done 结果）回传；或任务失败时对 0 消息会话做补偿删除；或会话列表/启动时清理本助手 0 消息会话。无论哪种，补一条"任务失败不残留空会话"的测试。

### 2. 前端发送的 POST 失败时，未送达的用户气泡仍留在界面

**位置**：`web/src/components/AgentPanel.vue:190-193`（先 push 用户气泡再发起 POST）、`:247-251`（catch 只设 `error` 与 `sending`，不回滚气泡）

`send()` 在调用 `chatProject` 之前就把用户消息推入 `messages`。若 POST 本身失败（409 忙碌、网络错误、会话/文档校验 400/404），消息从未持久化，但气泡与正常发送无异；刷新页面后气泡消失，界面与数据库出现短暂分叉，用户可能误以为消息已送达。注意这与 `task_failed` 路径不同——那条路径上用户消息已由 runtime 先行持久化，保留气泡是正确的。

**修复建议**：POST 失败的 catch 中移除本次 push 的气泡（或将其标记为"未发送，点击重发"）。补一条"POST 失败不残留未持久化气泡"的前端测试。

---

## P3 — 可优化

1. **`stream_chat_turn` 中途异常不显式关闭流**（`agent/llm.py:81-105`）：参数超限、流不完整等场景直接 `raise`，`AsyncStream` 依赖 GC 回收底层连接。建议 `try/finally` 中 `await stream.aclose()`（或改用上下文管理器），长驻服务下更稳。
2. **`tool_call` 事件早于 schema 校验发出**（`agent/runtime.py:310-313`）：JSON 解析成功即发 `tool_call`，而设计要求"完整参数通过 schema 校验后"再发。参数非法时 UI 会先闪现"Agent 正在准备修改"再失败，仅观感问题。
3. **第二轮未显式携带 `tools`/`tool_choice=none`**（`agent/runtime.py:368-373`）：不传 tools 时模型同样无法再调工具，与设计的 `tool_choice=none` 行为等价；但部分 OpenAI 兼容服务对"消息含 `role=tool` 而请求无 tools"可能拒绝，属兼容性观察点，建议在设计文档中注明实现取舍。
4. **聊天消息无长度上限**（`api/models.py:60`）：`ProjectChatRequest.message` 只有 `min_length=1`，而 `AgentTaskRequest.task` 有 `max_length=100_000`；消息会全量入库且每轮随完整历史进模型。单用户本地场景风险低，但建议对齐既有上限约定。
5. **`toolStatus` 在 task_done 后不清除**（`AgentPanel.vue:235-240`）："修改建议已生成"会一直停留到下次发送，建议在终态清空。
6. **流式输出期间不自动滚动**（`AgentPanel.vue:59-73` vs `:238-239`）：仅 task_done 时滚动到底部，长回复流式阶段需手动滚动。
7. **SSE 断线无重连/重放**（`web/src/api/client.ts:112-115`）：连接断开即终态报错；任务在服务端继续且成功后可经会话详情恢复，但用户需要手动刷新才能看到结果。本地场景可接受，建议至少在错误文案中提示"刷新可恢复"。
8. **模型返回空白回复时不持久化也不提示**（`agent/runtime.py:294-304`）：`reply.strip()` 为空则不写 assistant 消息，历史上出现连续 user 消息，界面无气泡。边缘情况，建议至少落一条空回复或在 UI 标注。
9. **`stream_chat_turn` 硬编码 `temperature=0.3`**（`agent/llm.py:66`）：与 `chat_text` 的带参风格不一致；项目聊天与选区改写共享同一温度，后续如需差异化需重构。

---

## 阶段 4 遗留项核对（本次全部闭环）

| 遗留项 | 当前状态 | 证据 |
|---|---|---|
| R1 写意图无 TTL + PID 复用或致文档永久阻塞 | 已修复 | `_WRITE_INTENT_TTL` 时间兜底（`projects.py:435-442`）+ `owner_started_at` 进程启动时间比对防 PID 复用（`projects.py:445-457`）；运行锁同机制（`store.py:528-551`） |
| `con .txt` 保留名放行 | 已修复 | `stem = part.split(".", 1)[0].rstrip(" .").upper()` 后再查保留名表（`projects.py:192-194`），`con .txt` 被拒 |
| selection-rewrites / agent/messages 无助手预检 | 已修复 | 两个端点入队前均 `validate_task_submission`（`api/main.py:241-245,278-294`），测试 `test_editing_task_endpoints_reject_unknown_or_busy_assistant_before_enqueue` 固化 |
| 崩溃残骸无清扫 | 已修复 | `MemoryStore.__init__` 启动即 `recover_project_artifacts`（`store.py:102`）+ 每次写入前 `_recover_write_intents` |

---

## 验证记录

- `pytest tests/test_memory_isolation.py`：**10 passed**（0.46s）。
- `pytest tests`：**156 passed**（22.41s），与声明基线一致。
- `npm test`：**41 passed**（8 个文件），其中 `AgentPanel.test.ts` 16 例覆盖会话恢复/切换/删除、流式单气泡、工具状态、失败清理、旧作用域丢弃。
- `npm run typecheck`（vue-tsc -b）：干净；`npm run build`：成功。
- 动态复现：孤儿空会话行为（P2-1）在独立临时库实测确认；会话删除的 pending/运行锁双保护、purge 级联均有对应测试且通过。
- 文档核对：架构 v1.15 变更说明与实现一致；`docs/` 四类目录约定（AGENTS.md 规则 13）未被违反；`.gitignore` 覆盖 `.env`/`data/`/构建产物，未发现敏感文件进入待提交集。

## 处理建议

按用户惯例，本次审查不改动任何代码。建议顺序：先修 2 个 P2（均为失败路径残留清理，改动面小且互相独立），P3 按精力择机处理；修复后补测试并全量回归。若确认 P3-3 的兼容性观察无需处理，可在设计文档补一句实现取舍说明。

另：本报告尚未登记进 `docs/README.md` 的审查记录表，待用户确认后一并补链。

---

## 处理结果（2026-08-12）

本报告提出的 2 个 P2 均已修复，并完成回归：

- P2-1：首次发送创建的新会话在任务写入首条消息前失败时，条件删除仍为 0 消息且无关联 change set 的会话；已有消息或 diff 的会话保持不变。
- P2-2：项目聊天 POST 未成功时，前端回滚本次未持久化的 user 气泡；SSE 任务失败时仍保留已由 Runtime 持久化的 user 消息。

P3 处理情况：

- 已处理 P3-1/2/4/5/6/7/8：异常显式关闭模型流；schema 校验通过后才发送 `tool_call`；聊天消息限制 100,000 字符；任务终态清理工具状态；token 流式滚动；断线移除未持久化半截回复并提示刷新恢复；空白回复转为可见且持久化的提示。
- 暂缓 P3-3：第二轮不携带 tools 已能禁止递归工具调用，是否为特定兼容服务显式传 `tool_choice=none` 需有真实兼容性证据后再调整协议。
- 暂缓 P3-9：`temperature=0.3` 与当前其他项目编辑调用一致；配置化会扩大设置契约，待出现不同场景温度需求时统一设计。

架构单一事实来源已升至 v1.16。修复后实测：记忆隔离 **10/10**、Python **162/162**、前端 **43/43**、`vue-tsc -b` 干净、生产构建成功。本文已登记进 `docs/README.md`。
