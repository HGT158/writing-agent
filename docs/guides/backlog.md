# 后续待办

本文件只记录已经确认但不属于当前完成范围的能力。实施任一事项前，必须重新设计、更新架构单一事实来源并按 TDD 完成回归。

## 所有 Agent 修改统一为编辑器内联 diff

状态：已确认设计，暂缓实施。

### 总则

所有由 Agent 对受管项目可编辑文档生成的修改，无论来自选区改写、项目聊天还是未来编辑工具，都必须先创建 `change_set`，禁止绕过 change set 直接写正文。正文只在用户明确接受且服务端完成版本、状态与原文快照校验后更新。本约束不改变长期记忆、项目元数据、项目外中间产物和完成态文章归档的既有写入语义。

### 粒度与数据模型

- change set 的唯一键为 `(task_id, document_id)`。同一次 Agent 任务对同一文档只允许提交一次编辑工具调用，该次调用必须包含对该文档的全部 hunk，并原子创建一个 change set；重复提交同一文档直接返回错误。涉及不同文档时每个文档各建一个 change set。
- `change_sets` 保存父级信息：`change_set_id`、`task_id`、助手/项目/文档/会话归属、来源、`base_version`、整体状态和时间戳。
- 新增 `change_set_hunks` 保存修改片段：`hunk_id`、`change_set_id`、`range_start`、`range_end`、`original_text`、`new_text`、`display_order`。一个 change set 可以包含多个 hunk，全部基于同一个 `base_version`。
- 范围统一使用 Python Unicode code point 的半开区间 `[start, end)`。相邻 hunk 合法，范围重叠非法；两个零长度插入不得位于同一位置。
- `display_order` 必须从 0 连续递增，只用于界面展示。服务端应用修改时只以实际范围位置为依据，从文档尾部向头部排序执行，不根据 `display_order` 计算位置。
- 每个 change set 最多 100 个 hunk；所有 hunk 的 `original_text + new_text` 按 UTF-8 编码后的总字节数最多 1 MiB。超限时整批拒绝，不创建部分记录。

现有单行 `change_sets` 数据迁移为“一个父级 change set + 一个 hunk”。历史记录没有 `task_id` 时生成确定性的合成值 `legacy-<change_set_id>`，不得使用 NULL，避免后续依赖 `(task_id, document_id)` 唯一键进行 upsert 时出现 SQLite NULL 互不冲突的问题。迁移必须在单个数据库事务中完成，任一步失败时完整回滚；同一次迁移内完成 `change_sets/change_set_hunks` 拆表、`(task_id, document_id)` 唯一索引、hunk 的 `(change_set_id, display_order)` 索引，以及删除 change set 时级联删除全部 hunk 的规则。TaskBroker 创建的真实 `task_id` 必须向 selection rewrite、项目聊天 Runtime 和编辑工具完整透传。

### 创建与冻结语义

- 模型可以流式生成工具参数，但参数 chunk 不落库，也不据此创建或修改 hunk。
- 编辑工具输入按文档分组，每个文档包含一个 `hunks` 列表，每个 hunk 只提供 `old_text` 和 `new_text`，不接受模型直接提供 offset。服务端在对应 `base_version` 正文中通过 `old_text` 唯一匹配计算 code point offset；任一 hunk 匹配不到或匹配多处时，整个工具调用失败，不创建任何 change set。选区改写可以使用服务端已经掌握的选区范围，但仍须校验该范围的原文快照。
- 工具调用参数完整结束后，服务端一次性完成 JSON/schema 校验、同一任务内文档重复提交校验、文档归属与版本校验、全部 hunk 原文快照校验、排序、不重叠和容量校验。
- 所有校验通过后，在一个短数据库事务中原子创建父级 change set 与全部 hunk；任一项失败则整批不创建。
- change set 创建完成即冻结，不允许后续追加或改写 hunk，因此不引入 `building` 状态。
- 工具提交成功、但后续可见说明回复失败时，已经创建的 change set 保持 pending，由用户决定接受或放弃。工具参数未完成、校验失败或在提交前中断时不保留半成品。

### 状态模型

```text
pending -> applied / rejected / stale
stale   -> rejected
```

- 状态在 change set 级别，第一版只支持整组接受或整组放弃，不引入 hunk 级状态。
- `stale` 是持久化状态，不做自动 rebase，也不做位置重映射。
- 文档当前版本不等于 `base_version` 时，change set 直接标记为 stale。
- 版本相同但任一 hunk 的原文快照不匹配时，视为存储或外部文件异常：记录 error 日志，并按 stale 处理。
- stale change set 禁止应用；用户可以查看旧 diff、将其放弃为 rejected，或基于当前正文重新生成一个新的 change set。重新生成不自动删除旧记录。
- 同任务同文档的修改已经合并，因此接受一个 change set 只会连带 stale 其他任务对该文档生成的建议。

### apply、写入意图与并发语义

多 hunk apply 必须沿用现有 `document_write_intents` 的可恢复三段式写入，禁止持有 SQLite 写锁执行磁盘 IO：

1. **登记短事务**：校验 `status == pending && document.version == base_version`，重新校验全部 hunk 快照，按范围倒序生成目标正文，并登记包含目标正文的 `document_write_intent`。
2. **事务外文件写入**：保留原文件 BOM 策略，通过临时文件和原子替换写入目标正文。
3. **终结短事务**：文档版本递增一次，当前 change set 置为 applied，并把同一文档其余版本不匹配的 pending change set 更新为 stale。

同一文档同一时刻只允许一个活跃写入意图。现有 `document_write_intents` 上的 `UNIQUE(assistant_id, project_id, document_id)` 是最终并发边界：登记时发现已有活跃 intent 必须立即返回 HTTP 409 `conflict`，不得依赖尚未递增的文档版本 CAS 阻止并发。两个客户端并发应用同一文档的不同 change set 时，只允许一个成功登记 intent，另一个必须在 intent 登记层收到 409。

reject change set 前必须在同一短事务中检查是否存在引用该 change set 的活跃 `document_write_intent`；存在时立即返回 HTTP 409 `conflict`，不得修改 change set 状态。intent 已登记后，写入和崩溃恢复的 applied 终结语义优先于任何后到的 reject，恢复流程必须把该 change set 终结为 applied，而不是接受或保留 rejected 状态。

崩溃恢复必须执行与正常第三段相同的 applied/stale 终结逻辑。服务端在文件替换后、终结事务前崩溃时，恢复流程根据 intent 的目标版本和内容摘要完成元数据终结，不能让正文、版本和 change set 状态永久分叉。

### API 与客户端状态对账

- 保存和 apply 的成功响应返回 `staled_change_set_ids: string[]`，客户端据此即时更新 App 层状态；该字段只是低延迟优化，不是唯一真相源。
- 提供按助手、项目和文档查询 change set 状态的只读 API，结果包含父级状态和全部 hunk，并支持分页。页面加载、SSE 重连或重新打开文档时，客户端必须遍历当前文档的全部分页，完整拉取相关 change set 状态后才能与本地 pending/stale/applied/rejected 状态对账，不得只读取首页。
- 如果服务端在文件替换后、返回响应前崩溃，客户端重新加载后必须从查询 API 得到恢复后的 applied/stale 终态，不能依赖已经丢失的单次响应体。
- apply 冲突返回 HTTP 409，并用稳定错误码区分 `stale`、`already_applied`、`already_rejected` 和 `conflict`；任何失败都不得修改正文。

### 编辑器显示规则

- 目标文档已打开、标签无未保存内容、版本和全部 hunk 快照匹配时，CodeMirror 必须一次渲染该 change set 的全部 hunk，每处显示删除态原文和新增态建议。整组接受/放弃按钮放在编辑器顶部横幅，不在每个 hunk 重复。
- 目标文档未打开时，侧栏和工作记录提供“打开并定位”入口；打开后立即渲染内联 diff。
- 标签存在未保存内容时不猜测位置，显示“本地正文尚未保存，无法安全定位”。用户保存后文档版本递增，建议按 stale 处理；用户放弃本地修改并恢复基准版本后，若快照仍匹配则可以继续显示。
- 内联 diff 渲染期间用户对该文档产生任何编辑时，立即撤下全部装饰并显示 dirty 提示，装饰不得跟随文本漂移。
- stale 建议不得在旧坐标强行绘制装饰；编辑器顶部显示 stale 审阅提示，并允许查看冻结的原始 diff、放弃或重新生成。
- 同一文档存在多个任务的有效建议时全部显示。接受其中一个后，其他建议因版本变化立即成为 stale，并按 stale 规则展示。

状态由 App 层统一持有。编辑器内联视图、侧栏索引和工作记录只展示同一份 change set 数据，不复制 diff，也不各自调用 API。侧栏以 change set 为条目，展开列出 hunk 摘要；点击 change set 时打开目标文档，点击具体 hunk 时滚动并高亮对应内联位置。

### 测试覆盖

- selection/chat 两种来源都创建父级 change set，并进入内联视图。
- 同任务同文档多处修改合并为一个 change set，hunk 有序、不重叠且使用 code point 范围。
- 工具参数流中断、任一 hunk 匹配或校验失败、同一任务重复提交同一文档时不创建半成品；一次性整批创建完成后 change set 立即冻结。
- 编辑工具按文档接收完整 `hunks` 列表，拒绝模型提供 offset；服务端从基准正文唯一匹配 `old_text` 计算范围，覆盖匹配不到、多处匹配和多 hunk 成功定位。
- 旧单行记录在单事务中迁移为单 hunk，并获得非空合成 `task_id`；迁移失败完整回滚，唯一索引、hunk 顺序索引和级联删除规则与拆表同时生效。
- hunk 数量、总字符量、重复 order、order 缺号、重叠范围及同位置空插入边界。
- 未打开文档的定位与打开后渲染；dirty 文档不错误定位；编辑期间立即撤下装饰。
- apply 后全部 hunk 生效、文档版本只递增一次；按范围倒序应用不产生偏移错误，覆盖相邻 hunk 和 Unicode 字符。
- 两个客户端并发 apply 同一文档的不同 change set 时，仅一个成功登记 intent，另一个在 intent 登记层收到 409 `conflict`。
- apply 已登记 intent 后并发 reject 同一 change set 时，reject 返回 409 `conflict` 且不修改状态；模拟崩溃并恢复后，该 change set 的终态为 applied 而不是 rejected。
- 保存或应用其他 change set 后自动标 stale，成功响应包含 `staled_change_set_ids`。
- stale apply 返回 409 `stale`，重复 apply/reject 返回稳定错误码，且均不修改正文。
- 文件替换后、终结事务前模拟崩溃；恢复后文档版本、applied change set 和连带 stale 状态一致。
- apply 响应丢失后客户端重新加载，通过文档 change set 查询完成 applied/stale 状态对账。
- 不执行自动 rebase 或位置重映射；接受一个 change set 后同文档其他任务建议全部 stale。
- 编辑器、侧栏和工作记录状态同步，并阻止重复点击发起并发请求。

### 明确不做

- 逐 hunk 接受或放弃。
- 自动 rebase 或位置重映射。
- hunk 级状态管理。
- 流式持久化未完成的 hunk。
- 同一任务对同一文档执行多次编辑工具调用；未来支持该能力时必须重新设计 change set 合并与冻结语义。

## Agent 聊天的持久化工作记录

状态：已确认设计，暂缓实施。

### 目标与边界

项目 Agent 聊天增加类似 Codex 的工作记录：运行时流式显示进度摘要、工具调用、工具结果、warning 和修改建议生成状态；任务运行时默认展开，任务结束后自动折叠，刷新或重新打开会话后保持可展开查看。最终 assistant 回复始终独立显示，不折叠进工作记录。

工作记录只用于界面展示，必须与聊天消息、模型上下文和上下文摘要完全隔离。`build_chat_context` 及摘要生成只能读取 `project_chat_messages`，不得读取工作记录表。

“思考过程”只表示可面向用户的简洁进度与决策摘要，例如“正在读取当前文档”“决定调用编辑工具”“正在校验修改范围”。不得保存或展示模型隐藏的原始推理链；Provider 没有提供可公开 reasoning summary 时，由编排层发出确定性的阶段进度，不伪造详细思维过程。

### 数据模型

新增 `project_chat_work_events`：

- `event_id`
- `assistant_id / project_id / chat_session_id`
- `task_id`
- `user_message_id`：归属到触发本轮任务的用户消息
- `event_seq`：任务内持久化事件顺序
- `kind`：`progress / tool / warning / changes / task`
- `status`：`succeeded / failed / interrupted`
- `change_set_id`：仅 `kind=changes` 使用；每条 changes 事件关联一个 change set 和一个文档
- `title / detail`
- `tool_name / args_summary / result_summary`
- `created_at / completed_at`

`event_seq` 在 `work_item_start` 时分配，以保留并行工具的发起顺序；完成落库可以乱序，进程硬退出后允许出现序号空洞。`(task_id, event_seq)` 必须唯一，并为 `kind=task` 建立 `(assistant_id, project_id, task_id)` 唯一部分索引，保证每个任务最多一个持久化终态。所有读写同时过滤 `assistant_id + project_id + chat_session_id`；会话删除、项目 purge 和助手 purge 必须级联清理工作记录。工作记录不进入 FTS、长期记忆、项目聊天摘要或模型 prompt。

### 流式协议与落库时机

- `work_item_start`：前端创建一条仅存在于内存的运行中工作项。
- `work_item_delta`：追加进度或公开思考摘要，只通过 SSE 传输，不落库。
- `work_item_done`：事件完成，前端更新同一个 `work_id`；服务端只在此时写入一条完整记录。

流式 chunk 永不落库。`tool_call` 映射为 `work_item_start(kind=tool)`，`tool_result` 更新同一个 `work_id` 并形成 `work_item_done`，最终数据库中只有一条合并后的工具记录，不分别保存 call/result 两行。

任务正常结束或可控失败时，仍处于运行中的工作项统一结束为 `interrupted` 后再落库。进程被强制终止、来不及形成完成事件时，不保存残缺 chunk 或伪造单项完成记录。任务终态作为 `kind=task` 的完成事件持久化，用于恢复后判断工作记录已经结束。

第一版不引入持久化 `work_run` 表。应用加载、页面恢复或客户端重连时，服务端必须按 `task_id` 对账工作事件：事件组没有 `kind=task` 终态事件，且 TaskBroker 中不存在对应活动任务时，在短事务中以 `event_seq = max(event_seq) + 1` 补写一条 `status=interrupted` 的 `kind=task` 终态事件；仍有活动任务时保持运行中，不得提前终结。正常终结与恢复对账都通过 `kind=task` 唯一部分索引执行幂等插入，并发时只有第一条终态写入成功，后续写入读取并复用已存在的终态。

### 截断、脱敏与数量上限

- 工具结果最多持久化 8,000 个 Unicode 字符，超出后保留前 6,000 字符和后 2,000 字符，并记录原始字符数与 `truncated=true`。结构化结果中的敏感字段同样必须先脱敏再截断。
- 工具参数摘要最多 4,000 个 Unicode 字符。写入前递归脱敏名称匹配 `api_key`、`token`、`authorization`、`cookie`、`secret`、`password` 等字段的值。
- 单个任务最多持久化 199 条非任务终态明细事件；第 200 条位置固定保留给溢出摘要事件。超过 199 条后不再持久化新的明细，任务结束时把省略事件按类型合并为第 200 条“省略 N 条记录”摘要；没有溢出时不创建该摘要事件。
- `kind=task` 的任务终态不受 200 条限制，必须尽力持久化。
- 每个 change set 单独生成一条 `changes` 工作事件。该事件保存对应 `change_set_id`、文档标识和“为该文档生成了 N 个 hunk 修改建议”等摘要，不复制 hunk 或正文 diff；一次工具调用涉及多个文档时生成多条 changes 事件，`change_sets/change_set_hunks` 仍是修改内容的唯一真相源。

### 会话恢复与界面行为

- 会话详情 API 将聊天消息和工作记录分字段返回。前端按 `user_message_id + task_id` 把工作记录放在对应用户消息与最终 assistant 回复之间，但不得把它们合并成聊天 message。
- 任务运行期间工作记录默认展开，并随着 `work_item_delta` 继续更新；用户可以手动折叠或重新展开。
- 收到 `task_done` 或 `task_failed` 后自动折叠，标题显示耗时、工具数、修改建议数和最终状态。
- 页面刷新或重新打开历史会话时，已完成工作记录默认折叠，可手动展开查看持久化事件；不恢复未落库的流式 chunk。
- 应用加载、页面恢复或客户端重连时先执行任务终态对账；没有终态且 TaskBroker 中不存在活动任务的事件组补写 interrupted 终态后，按已完成工作记录默认折叠。
- tool 工作项在调用期间显示 running，收到结果后在原位置更新为 succeeded/failed，不新增第二条记录。
- 点击 `changes` 工作项时定位到对应 change set；目标文档未打开时先打开，状态为 stale 时只展示冻结 diff，不尝试 rebase。

### 测试覆盖

- `work_item_delta` 不落库，`work_item_done` 才产生一条记录。
- tool_call/tool_result 在 UI 和数据库中合并为同一个 work item，并正确更新运行、成功和失败状态。
- 工具参数脱敏、4,000 字符参数上限、8,000 字符结果上限及首尾截断标记。
- 单任务最多 199 条明细、第 200 条固定溢出摘要、无溢出时不生成摘要，以及不受该限制的任务终态。
- 正常完成、任务失败、取消和可控中断时的工作项终结；强制进程退出不保存残缺 chunk。
- 部分完成事件已经落库、进程在任务终态前退出时，重启或重连后因 TaskBroker 中不存在活动任务而幂等补写 interrupted 终态。
- 工作记录按助手、项目、会话和用户消息隔离，并在会话/项目/助手删除时级联清理。
- 会话恢复后工作记录仍可展开，但构造模型上下文和生成摘要时完全不包含工作记录内容。
- 运行时默认展开、终态自动折叠、历史恢复默认折叠及用户手动切换。
- 每个 changes 工作事件只关联一个 change set；对应链接可以打开该文档、定位有效内联 diff，并按 stale 规则降级。一次工具调用产生多个 change set 时，各自事件和链接互不混用。

## 已完成并移出待办

- SSE 断线游标续传已在 v1.18 实现：数据帧带标准 `id: <seq>` 行，流端点接受 `after_seq` / `Last-Event-ID` 游标，游标落后于窗口时发送 `reconnect_gap` 缺口信号；前端按退避自动重连、按 `seq` 去重，缺口后等待终态并重载持久化会话。现行契约见架构文档 §5.9/§5.10。
- 长会话上下文压缩已在 v1.17 实现：按 token 预算保留最近消息、持久化增量摘要并对当前文档正文做窗口截断。现行契约见架构文档 §3.3。
