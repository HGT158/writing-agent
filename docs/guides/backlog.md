# 后续待办

本文件只记录已经确认但不属于当前完成范围的能力。实施任一事项前，必须重新设计、更新架构单一事实来源并按 TDD 完成回归。

## 暂不处理的观察项（截至 2026-08-22 phase8 复议）

- 第二轮 `tool_choice=none`（phase6 遗留）：项目聊天工具调用后追加的说明轮次不携带 tools，属既定契约；是否改显式 `tool_choice=none` 待模型服务商对该参数行为稳定后再评估。
- 温度硬编码 0.3/0.2（phase6 遗留）：`agent/llm.py` 与 `agent/runtime.py` 的模型调用温度未配置化；需要按场景区分温度时再引入 `.env` 配置。
- 中文标点未计入 CJK 估算（P3-4）：软阈值轻微滞后，无实际危害；下次动 `agent/context.py` 时可顺手把 U+3000–U+303F、U+FF00–U+FFEF 并入正则。
- 选区工具栏不随编辑器滚动（P3-5）：纯观感，重选或 Esc 即恢复；需要时在 `geometryChanged` 重算坐标。
- `default` 助手删除按钮不预先禁用（P3-7）：架构要求"服务端拒绝原样提示"，现状合规；预先禁用属可选体验优化。
- broker 订阅者队列无上界（P3-8）：本地单用户场景无风险；若未来开放远程访问，需加有界队列 + 慢消费者降级。
- 上下文预算最终截断分支（phase7 P3-1）：截断标注未计入 allowance 且截断后不复核；默认配置下仅有小幅估算偏差，下次调整 `agent/context.py` 时一并收紧。
- 旧 schema 转换函数（phase7 P3-2）：`memory/projects.py::_row_to_change_set` 已无调用且字段仍是旧结构；当前不可达，下次整理项目存储层时删除。
- Memory 层分页上限（phase7 P3-4）：API 已限制 `page_size <= 100`，`memory/projects.py` 仅校验正数；当前调用方均经 API，本地单用户风险低，下次扩展直接调用方时同步 clamp。
- 写意图 finalize 返回契约（phase7 P3-5）：`_finalize_write_intent` 注解仍为 `None`，实际返回 stale 列表，意图缺失分支返回 `None`；当前正常路径可达性低，下次修改写意图时统一为 `list[str]` 与空列表。
- 任务终态后的防御性调用（phase7 P3-7）：`finish_task` 后若错误地再次 `start`，可能复用已落库序号；Runtime 当前无该调用顺序，下次调整工作记录生命周期时显式拒绝。
- 跨项目文档标签回退（phase7 P3-8）：Agent 作用域来自活动标签、资源管理器停在其他项目时，侧栏目标文件可能回退显示“当前文档”；不影响打开与 hunk 定位，仅属标签观感。
- 键盘可达性与主题细节（phase7 P3-9）：主题菜单缺方向键导航和打开后聚焦，工作记录的可点击修改项仍使用无键盘语义的 `li`，`ThemeDefinition.dark` 暂未消费；首帧主题闪烁受 CSP 禁止内联预热脚本约束。后续单独做无障碍与首屏体验整理。
- SSE 反复开连即断（phase7 P3-10）：`watchTask` 在 `onopen` 后重置退避，服务端若持续开连后立即断开可无限以 500ms 重连；普通连接失败已有 6 次上限，若出现该异常模式再增加总次数或总时长边界。
- 剩余专项测试（phase7 P3-11）：可补 CancelledError 经真实 recorder 的 interrupted 分支、并发终态对账、迁移成功后二次启动、跨助手持 change set id 返回 404、工作事件断线补发；字符串形态脱敏与同组 reject 后 accept 已随 v1.23 修复覆盖。
- `summarize_detail` 截断标注口径（phase8 P3-2）：“原始 N 字符”的 N 是脱敏后长度，发生脱敏时比真实原文短；下次改 `agent/work_log.py` 截断逻辑时改为“脱敏后 N 字符”或去掉口径标注。
- 非 JSON 文本载荷的值级扫描（phase8 P3-3）：`args_summary`/`result_summary` 的非 JSON 普通文本按架构保持原文、不做值级脱敏（仅 detail 做）；接入可能返回内嵌凭据纯文本的新 MCP 工具前需评估扩围。
- ChangeDiff 缺 Space 键激活（phase8 P3-5）：`role="button"` 只监听 Enter，未处理 Space（WAI-ARIA button 惯例两者都要）；与 phase7 P3-9 无障碍暂缓项同族，并入后续无障碍整理。
- `agent/work_log.py` import 分组（phase8 P3-6）：`import logging` 未与其他标准库导入归组；纯格式，下次改动该文件时顺手调整。

## 已完成并移出待办

- 压缩 `info`/`warning` 前端可见性（phase6 P3-1）已由 v1.19 工作记录解决：压缩提示进入工作记录的进度/警告条目，无需单独事件分支。
- 多 hunk change set 与逐 hunk 审查已在 v1.20 实现：`change_sets` 父表 + `change_set_hunks`（单事务迁移、`legacy-<id>` 合成任务 id、`(task_id, document_id)` 唯一）；`propose_project_edits` 按文档分组接收 hunks（同文档多处一次提交，修复"每个文档只能出现一次"缺陷，≤100 hunk / ≤1 MiB、创建即冻结）；接受单个 hunk 为唯一应用原语（三段式写入、版本 +1），同组其余 hunk 以 `old_text` 内容复检保持可审，其他任务建议整组 stale；API 提供 hunk 级 accept/reject、accept-all 与按文档分页查询（稳定错误码 + `staled_change_set_ids`）；前端内联 diff 一次渲染全部 hunk、每个 hunk 自带独立接受/放弃按钮（TRAE 式），侧栏卡片按 hunk 摘要展示并提供批量入口。现行契约见架构文档 §4.7/§5.9/§5.10。
- 项目聊天持久化工作记录已在 v1.19 实现：`project_chat_work_events` 表、`work_item_start/delta/done` SSE 事件（delta 不落库、done 落库，单任务 199+1 上限、参数 4,000/结果 8,000 字符脱敏截断）、失败/取消 interrupted 终结、会话详情按 TaskBroker 活动对账补写终态、前端运行中展开终态折叠。现行契约见架构文档 §5.4/§5.7/§5.9/§5.10。
- SSE 断线游标续传已在 v1.18 实现：数据帧带标准 `id: <seq>` 行，流端点接受 `after_seq` / `Last-Event-ID` 游标，游标落后于窗口时发送 `reconnect_gap` 缺口信号；前端按退避自动重连、按 `seq` 去重，缺口后等待终态并重载持久化会话。现行契约见架构文档 §5.9/§5.10。
- 长会话上下文压缩已在 v1.17 实现：按 token 预算保留最近消息、持久化增量摘要并对当前文档正文做窗口截断。现行契约见架构文档 §3.3。
