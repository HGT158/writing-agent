# 后续待办

本文件只记录已经确认但不属于当前完成范围的能力。实施任一事项前，必须重新设计、更新架构单一事实来源并按 TDD 完成回归。

## 已完成并移出待办

- 多 hunk change set 与逐 hunk 审查已在 v1.20 实现：`change_sets` 父表 + `change_set_hunks`（单事务迁移、`legacy-<id>` 合成任务 id、`(task_id, document_id)` 唯一）；`propose_project_edits` 按文档分组接收 hunks（同文档多处一次提交，修复"每个文档只能出现一次"缺陷，≤100 hunk / ≤1 MiB、创建即冻结）；接受单个 hunk 为唯一应用原语（三段式写入、版本 +1），同组其余 hunk 以 `old_text` 内容复检保持可审，其他任务建议整组 stale；API 提供 hunk 级 accept/reject、accept-all 与按文档分页查询（稳定错误码 + `staled_change_set_ids`）；前端内联 diff 一次渲染全部 hunk、每个 hunk 自带独立接受/放弃按钮（TRAE 式），侧栏卡片按 hunk 摘要展示并提供批量入口。现行契约见架构文档 §4.7/§5.9/§5.10。
- 项目聊天持久化工作记录已在 v1.19 实现：`project_chat_work_events` 表、`work_item_start/delta/done` SSE 事件（delta 不落库、done 落库，单任务 199+1 上限、参数 4,000/结果 8,000 字符脱敏截断）、失败/取消 interrupted 终结、会话详情按 TaskBroker 活动对账补写终态、前端运行中展开终态折叠。现行契约见架构文档 §5.4/§5.7/§5.9/§5.10。
- SSE 断线游标续传已在 v1.18 实现：数据帧带标准 `id: <seq>` 行，流端点接受 `after_seq` / `Last-Event-ID` 游标，游标落后于窗口时发送 `reconnect_gap` 缺口信号；前端按退避自动重连、按 `seq` 去重，缺口后等待终态并重载持久化会话。现行契约见架构文档 §5.9/§5.10。
- 长会话上下文压缩已在 v1.17 实现：按 token 预算保留最近消息、持久化增量摘要并对当前文档正文做窗口截断。现行契约见架构文档 §3.3。
