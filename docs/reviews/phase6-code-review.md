# 阶段 6 代码审查报告

> 审查对象：`writing-agent/` 阶段 5 定稿提交（`288e78a`）之后的全部已提交改动，即 v1.16 项目聊天上下文管理（Token 估算 + 摘要压缩）、v1.17 活动 SSE 跨事件滑窗 / 编辑器内联 diff 双视图 / 选区工具栏可输入 / 前端助手增删、v1.18 SSE 断线游标续传与 fetch MCP server 部署调整，共 5 个提交（`ec9972a`、`98f802e`、`cd3896e`、`4272d1b`、`f35495b`），41 个文件、+2793/−561 行
> 审查日期：2026-08-16
> 审查方式：逐文件静态走读全部 diff 与改动后现状代码 + 架构文档（v1.18）契约逐条比对 + 新增测试覆盖度核对。按用户要求，本次未改动任何代码，也未重跑 pytest/vitest
> 环境：Windows 11 / 与 AGENTS.md 声明环境一致

---

## 总体评价

先说做得好的部分：

- **SSE 续传设计严谨、实现与契约逐条对齐**：`TaskBroker`（`api/tasks.py:32-164`）以任务内单调 `seq` 寻址，有界滑窗裁剪只影响重放起点不影响活动订阅者；游标语义（`after + 1` 精确补发、落后窗口发 `reconnect_gap`、未来游标回拨至多重发末尾事件、非法 `Last-Event-ID` 按全新订阅）在 `stream()` 中逐条可对应，且 `test_task_broker.py` / `test_api_management.py` 用独立用例固化了跨窗口收流、重连去重、显式参数与请求头优先级、SSE `id:` 帧格式。
- **断线缺口的前端失败语义正确**：`watchTask`（`web/src/api/client.ts:111-184`）按退避重连、按 `seq` 去重、终态自动关闭；`reconnect_gap` 后订阅层丢弃一切非终态事件，AgentPanel 移除半截 assistant 回复并在终态后从持久化会话恢复完整内容与漏发的 pending diff（`AgentPanel.vue:252-293`），选区改写保留可重试提示（`DocumentEditor.vue:177-181`）。不静默拼接残缺回复，符合架构 §5.9/§5.10。
- **上下文压缩职责划分干净**：`agent/context.py` 独立承载估算、截断与切分，Runtime 只决定是否落库摘要；压缩失败降级为丢弃窗口外消息且绝不阻断本轮聊天（`context.py:133-146`）；摘要以三键隔离落 `project_chat_summaries`，会话删除、空会话补偿删除、项目 purge、助手 purge 四处级联清理全部补齐并有测试（`tests/test_project_chat_history.py` 新增 3 组用例）；`token_budget=0` 关闭压缩恢复 v1.15 全量行为，`.env.example` 与架构 §3.3 同步。
- **内联 diff 双视图遵守单一状态源**：pending 集合由 App 层唯一持有（`App.vue:36-62`），编辑器装饰只读、正文仍仅在 apply 成功后由服务端内容同步；版本不符或 dirty 时降级为提示不在错误位置画 diff（`DocumentEditor.vue:42-58`）；接受/拒绝统一走父级通道，两侧同忙同删，`App.test.ts`/`DocumentEditor.test.ts` 新增用例覆盖双视图同步、失败保留、busy 禁用。
- **阶段 5 的两个 P2 修复仍在位且被测试固化**：首发消息任务失败的 0 消息会话补偿删除（`api/main.py:314-327` + `memory/store.py:228-234`，`tests/test_api_projects.py:462-470` 验证补偿失败不掩盖任务错误）；POST 失败回滚未持久化 user 气泡（`AgentPanel.vue:301-311`）。
- **MCP fetch 修复决策合理且文档完备**：弃 uvx 改 conda 环境直跑、`mcp` SDK 钉 `>=1.10,<2` 的原因（2.0 改名 `McpError` 而 mcp-server-fetch 未适配）在提交说明与架构 §5.6 写清楚；`requirements.txt` 同步增删，`uv` 依赖移除。
- **文档纪律保持**：架构单一事实来源升至 v1.18，§3.3/§5.4/§5.6/§5.9/§5.10/§6.2/§9 风险表全部更新；`docs/README.md` 重排"现行依据 / 版本化专题设计 / 审查记录 / 历史设计"并登记 phase5；backlog 补全多 hunk 统一设计；README 基线更新至 185/10/70。

本阶段发现 **0 个 P0、0 个 P1、1 个 P2、10 个 P3**。没有数据损坏、越权或安全问题；新表隔离、滑窗 seq 寻址、游标补发、级联清理等关键路径经走读确认安全。问题集中在超长消息下上下文预算缺最后兜底（P2），以及事件可见性、前后端校验口径、pending 集合生命周期等健壮性/体验细节（P3）。

测试现状（引自 AGENTS.md 声明基线，本次按用户要求未重跑）：

```
Python 185/185 · 记忆隔离 10/10 · 前端 70/70 · vue-tsc 与生产构建通过
本次新增覆盖：test_chat_context.py（9 例）、test_task_broker.py（+6 例）、
test_api_management.py（+1 例）、test_runtime_project_editing.py（+4 例）、
test_project_chat_history.py（+2 例）及前端 5 个测试文件共 +36 例
```

---

## P0 — 阻断验收

未发现。

## P1 — 应修复

未发现。以下高风险路径已逐一核查确认安全，列出以备追溯：

- **滑窗裁剪不吞终态**：终态事件是任务的最后一条事件，其后不再有新增，因此永远停留在窗口内；重连重放必然送达（`api/tasks.py:141-147`），`test_task_broker_streams_beyond_event_window` 用 `max_events=4` 极端窗口固化。
- **重放与活动流的重复/遗漏**：订阅先于快照建立，重叠区间由 `cursor` 单调推进去重；`after_seq` 负值钳到 `-1`、超界钳到 `next_seq-2`，不产生空流也不越界（`api/tasks.py:122-140`）。
- **摘要的三层隔离**：`project_chat_summaries` 全部读写均带 `assistant_id + project_id + chat_session_id` 参数化绑定；`save_summary` 先经 `_session_row` 校验会话存在且未归档；摘要是派生数据，不进会话详情的可见历史（架构 §5.7 契约与实现一致）。
- **补偿删除与摘要清理**：`delete_empty_session` 仅在消息数为 0 且无关联 change set 时删会话，同事务顺带清理摘要行（`memory/project_chat.py:343-382`），不会误删已产生内容的会话。
- **XSS**：本阶段未新增任何 `v-html`；助手消息复用既有 `MarkdownPreview`（dompurify 消毒），内联 diff 与工具栏控件均 `textContent` 插入。
- **助手删除链路**：前端二次确认 + 归档语义提示 + 仅剩一个时禁用；服务端 `default` 拒删与运行锁预检不变，409/400 原样透传（架构 §5.10 "前端不猜测原因"）。

---

## P2 — 建议修复

### 1. 保留窗口无总量兜底：超长聊天消息可撑破模型上下文使整轮失败

**位置**：`agent/context.py:111-127`（预算检查与 `recent` 切分）、`api/models.py:60`（单条消息上限 100,000 字符）、`agent/runtime.py:294-322`（预算参数注入）

`build_chat_context` 的兜底逻辑是"预算内全量发送；超预算则保留最近 `keep_recent` 条全文、压缩更早部分"。但当超预算的原因是**保留窗口本身过大**时（`older` 为空，或 `recent` 自身已超过预算），函数直接原样返回全量 `baseline`（`context.py:120-127`），没有任何二次裁剪。单条聊天消息允许 100,000 字符（约数万 token，CJK 估算下即可能单独超过 24k 预算），写作场景下用户向聊天粘贴大段正文是合理用法；连续几次大粘贴后，最近 8 条消息的总量可以远超任何常见模型的上下文窗口。

**失败形态**：请求发给模型后被服务端 400 拒绝，任务以 `task_failed` 结束、错误文案为供应商原始报文；用户消息已持久化，无数据丢失，但该会话在出现新的压缩点之前会**每轮稳定失败**，且提示不可读。触发概率不高，但一旦触发无法自愈，属于可用性漏洞而非观感问题。

**修复建议**（三选一，需设计决策）：给进入 prompt 的单条消息也套用类似 `clip_document_content` 的首尾窗口截断并显式标注省略；或当 `recent` 自身超预算时继续收缩窗口（始终保护最新一条 user 消息）；或对保留窗口设字符总量硬上限。无论哪种，补一条"8 条满额超长消息不产生超预算 prompt"的单测。注意截断只影响 prompt，不得影响可见历史与 `propose_project_edits` 的服务端精确匹配（与 §3.3 文档截断同一原则）。

---

## P3 — 可优化

1. **压缩 `info`/`warning` 事件在 Web 端不可见**（`agent/runtime.py:303-318` 发出，`web/src/components/AgentPanel.vue:246-293` 无对应分支）：架构 §5.4 写明压缩时发 `info` 是"便于用户理解上下文被折叠"，但 AgentPanel 只处理 token/reconnect_gap/tool_call/tool_result/change_preview/终态，`info` 与"压缩失败已丢弃 N 条消息"的 `warning` 在浏览器里被静默丢弃，仅 CLI 的 `console_printer` 可见（`agent/events.py:86-89`）。建议 AgentPanel 对 `warning` 至少给一条可见提示（压缩失败意味着模型本轮看不到被丢的历史），`info` 可做成轻提示或维持现状但在文档注明 Web 不渲染。
2. **前后端助手 id 校验口径不一致**（`web/src/components/AssistantDialog.vue:16` vs `agent/assistant_registry.py:76`）：后端 `^[a-z0-9][a-z0-9_-]{0,49}$` 允许下划线，前端 `/^[a-z0-9][a-z0-9-]*$/` 不允许。UI 比 CLI/API 更严不算 bug，但同一能力两处规则分叉：界面建不了 `tech_writer`，` assistants create` 命令行却可以。建议二选一对齐并在错误文案中写同一套规则。
3. **`${LOCAL_PROXY}` 未设置时的 warning 文案误导**（`mcp_client/registry.py:27-30`）：该变量按 `.env.example` 说明是可选、缺省即直连；空串代理在 httpx/urllib 下等价于未设置，fetch server 行为正常，但 `_expand` 会记"server 可能启动失败"的 warning，每次启动产生一条噪音日志。建议对"引用存在但允许为空"的变量降级为 debug 或改写文案。附带观察：`config/mcp_servers.json` 硬编码 `C:/miniconda/envs/writing-agent/python.exe` 绝对路径，虽与 AGENTS.md 固定环境约定一致，但换机/重建环境时它是隐蔽的坏点，建议在 §5.6 或 README 环境节补一句"重建后需同步此路径"。
4. **`estimate_tokens` 未把中文标点计入 CJK**（`agent/context.py:12`）：正则覆盖 CJK 统一表意文字与日韩音节，但 `，。「」`（U+3000–U+303F）和全角符号（U+FF00–U+FFEF）按 4 字符/token 折算，纯中文文本的 token 数被系统性低估，压缩触发点略滞后于真实占用。预算本就是软阈值，影响很小，建议有空把两个区段并入正则即可。
5. **选区工具栏不随编辑器滚动**（`web/src/components/DocumentEditor.vue:108-118`）：`left/top` 用选区确定瞬间的视口坐标换算，打开工具栏后滚动编辑器，浮层停留在旧位置。重选或 Esc 可恢复，纯观感问题；可在 updateListener 里对 `geometryChanged`/scroll 重算或暂时隐藏。
6. **pending 集合的生命周期与"按活动标签确定的 Agent 作用域"不一致**（`web/src/App.vue:36-62`、`:121-122`，`web/src/components/AgentPanel.vue:51`、`:388-408`）：两个方向的错位——其一，`selectProject`（资源管理器切换选中项目）无条件清空整个 pending 集合，而此时编辑器/Agent 面板可能仍绑定着另一个项目的活动标签，UI 里的建议卡片消失（数据库仍在，切会话可找回）；其二，切换文档标签换到另一个项目时，旧项目的选区改写建议不会被清理，`addChange` 不按项目过滤，AgentPanel 卡片列表渲染全部 pending，`documentLabels` 查不到时回退成"当前文档"，跨项目卡片混排。影响有限（apply/reject 均以 change set 自身归属提交，不会错改文档），建议把 pending 的展示按 `agentProjectId` 过滤，清理时机与 Agent 作用域对齐。
7. **`deleteAssistant` 对 `default` 助手不预先禁用**（`web/src/App.vue:99-111`，`agent/assistant_registry.py:108-109`）：`default` 是后端固定拒删项，前端只按"剩余 ≥2 个"启用按钮，选中 default 时点击必然收到服务端错误。架构要求"服务端拒绝原样提示、前端不猜测原因"，现状合规；但这是唯一可预知的固定拒绝，预先禁用并加 tooltip 体验更好，属可选项。
8. **broker 订阅者队列无上界**（`api/tasks.py:25`、`:58-59`）：`asyncio.Queue()` 无 maxsize，若某个 SSE 客户端长期停止读取而任务持续产出 token，事件在该队列中无界积压。本地单用户场景风险很低，`StreamingResponse` 正常消费时不会发生；记录为观察点，若日后开放远程访问建议加有界队列 + 慢消费者降级（丢弃并转 `reconnect_gap` 语义）。
9. **README 相关文档仍链接阶段 4 审查报告**（`README.md:126`）："阶段 4 复审处理结果"链接未随 phase5 更新，`docs/README.md` 的审查记录表已有 phase5，两处不同步。小文档维护项。
10. **Runtime 用 `assert` 做类型收窄**（`agent/runtime.py:306-307`）：`python -O` 下断言被剥离会使后续对 `None` 的属性访问变成运行时错误。当前启动方式不受影响，建议改成显式 `if ... is None: raise` 或直接依赖 `summary_changed` 的返回结构，消除对断言开关的隐式依赖。

---

## 阶段 5 遗留项核对

| 遗留项 | 当前状态 | 证据 |
|---|---|---|
| P2-1 任务失败残留孤儿空会话 | 修复仍在位 | `operation()` 的 `finally` 对新会话幂等条件清理（`api/main.py:314-327`），`delete_empty_project_chat_session`（`memory/store.py:228-234`）本阶段又补了摘要行级联；`test_api_projects.py:462-470` 固化 |
| P2-2 POST 失败残留未持久化气泡 | 修复仍在位 | `AgentPanel.vue:301-311` 按乐观索引与内容双校验回滚 |
| P3-3 第二轮是否显式 `tool_choice=none` | 维持暂缓 | 第二轮仍不携带 tools（`agent/runtime.py:433-438`），与 phase5 处理结果一致 |
| P3-9 温度硬编码 | 维持暂缓 | `chat_text` 默认 0.3（`agent/llm.py:126`），摘要调用显式 0.2（`agent/runtime.py:229`），未做配置化，与 phase5 决定一致 |
| 选区改写断线缺口后无法找回漏发建议 | 维持暂缓（已有归口） | AGENTS.md 已知暂缓项 + `docs/guides/backlog.md` 多 hunk 设计的"API 与客户端状态对账"一节覆盖 |

---

## 验证记录

- 本次为纯静态走读（用户明确要求不重跑测试）：逐文件读完 `288e78a..HEAD` 全部 41 个改动文件的 diff 与关键文件的现状全文（`agent/context.py`、`agent/runtime.py` 聊天段、`api/tasks.py`、`api/main.py` 聊天/流端点、`memory/project_chat.py`、`web/src/api/client.ts`、`AgentPanel.vue`、`DocumentEditor.vue`、`App.vue`、`inlineDiff.ts`、`frozenSelection.ts`、`SelectionToolbar.vue`、`AssistantDialog.vue`、`mcp_client/registry.py` 等）。
- 契约比对：架构 v1.18 的 §3.3（分层压缩）、§5.6（fetch 部署）、§5.9（seq 寻址与游标续传）、§5.10（双视图/工具栏/助手管理）、§6.2（帧格式）、§9 风险表新增 8 行逐条与实现对照，未发现"只改文档不改行为"或反向情形。
- 测试覆盖核对（读测试不跑测试）：上下文压缩的预算内/超预算/增量合并/失败降级/空摘要 5 类路径，broker 的跨窗口/重连/游标/帧格式 6 例，压缩集成与文档截断 4 例，摘要隔离与级联清理 3 例，前端重连去重/缺口恢复/双视图/工具栏聚焦 36 例，均在位。
- 敏感信息核对：`.env` 与 `data/` 经 `git check-ignore` 确认被忽略，`config/__pycache__`、`.pytest_cache` 同样未入库；`.env.example` 只含占位值；`config/mcp_servers.json` 无密钥。
- 工作树状态：审查开始时 `git status` 干净，本报告针对已提交代码；审查进行中出现了一组未提交的"工作日志"功能改动（`agent/work_log.py`、`tests/test_work_log.py` 及 runtime/store/project_chat 等相关修改），与本审查无关，不在本报告覆盖范围内。

## 处理建议

按用户惯例，本次审查不改动任何代码。建议顺序：P2 涉及一次小的设计决策（保留窗口兜底策略三选一），确认方案后按 RED → GREEN 补测试并全量回归；P3 中 1、2、6 与用户可见行为相关可优先，其余按精力择机。P3-3（LOCAL_PROXY warning）与 P3-9（README 链接）属于顺手项，可与下一次功能改动一并处理。

另：本报告尚未登记进 `docs/README.md` 的审查记录表，也未改动 README 相关链接，待用户确认后一并补。
