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

## 已完成并移出待办

- 项目聊天持久化工作记录已在 v1.19 实现：`project_chat_work_events` 表、`work_item_start/delta/done` SSE 事件（delta 不落库、done 落库，单任务 199+1 上限、参数 4,000/结果 8,000 字符脱敏截断）、失败/取消 interrupted 终结、会话详情按 TaskBroker 活动对账补写终态、前端运行中展开终态折叠。现行契约见架构文档 §5.4/§5.7/§5.9/§5.10。
- SSE 断线游标续传已在 v1.18 实现：数据帧带标准 `id: <seq>` 行，流端点接受 `after_seq` / `Last-Event-ID` 游标，游标落后于窗口时发送 `reconnect_gap` 缺口信号；前端按退避自动重连、按 `seq` 去重，缺口后等待终态并重载持久化会话。现行契约见架构文档 §5.9/§5.10。
- 长会话上下文压缩已在 v1.17 实现：按 token 预算保留最近消息、持久化增量摘要并对当前文档正文做窗口截断。现行契约见架构文档 §3.3。
