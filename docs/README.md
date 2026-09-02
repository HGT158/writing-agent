# 文档导航

本目录按“现行依据优先、审查记录可追溯、历史设计不混入现行规范”整理。运行时架构与跨模块契约只以架构文档为准。

## 现行依据

| 文档 | 用途 |
|---|---|
| [architecture/phase1-architecture.md](architecture/phase1-architecture.md) | 架构单一事实来源（v1.33） |
| [../AGENTS.md](../AGENTS.md) | Agent 工作约定、环境和硬性规则 |
| [guides/new-session-prompt.md](guides/new-session-prompt.md) | 新对话的可复制启动提示词 |
| [guides/windows-task-scheduler.md](guides/windows-task-scheduler.md) | Windows 登录启动 Scheduler 的配置 |
| [guides/backlog.md](guides/backlog.md) | 已确认但暂缓实施的后续能力 |

## 版本化专题设计

下列文档记录具体版本的设计与实施边界，用于追溯决策，不是当前架构的单一事实来源；后续版本已经改变其中部分契约，冲突时以 v1.32 主架构为准。

| 文档 | 版本范围 |
|---|---|
| [architecture/project-agent-streaming-edit-design.md](architecture/project-agent-streaming-edit-design.md) | v1.13–v1.14 项目 Agent 流式编辑工具设计 |
| [architecture/project-chat-history-design.md](architecture/project-chat-history-design.md) | v1.15 项目 Agent 多会话历史设计 |

## 审查记录

审查报告按阶段保留，用于追溯问题、修复和当时的验证结果。报告中的版本号与测试数量是历史事实，不会因后续阶段完成而回写。

| 文档 | 范围 |
|---|---|
| [architecture/phase1-architecture-review.md](architecture/phase1-architecture-review.md) | 阶段 1 架构审查 |
| [reviews/phase2-code-review.md](reviews/phase2-code-review.md) | 阶段 2 MVP 代码审查 |
| [reviews/phase3-code-review.md](reviews/phase3-code-review.md) | 阶段 3 Memory + Scheduler 审查与二轮复审闭环 |
| [reviews/phase4-code-review.md](reviews/phase4-code-review.md) | 阶段 4 写作 IDE 审查与处理结果 |
| [reviews/phase5-code-review.md](reviews/phase5-code-review.md) | 项目 Agent 流式编辑与多会话增量审查及处理结果 |
| [reviews/phase6-code-review.md](reviews/phase6-code-review.md) | v1.16–v1.18 已提交改动复审；P2 与多数 P3 已在 v1.21 处理，其余见 backlog 观察项 |
| [reviews/phase7-code-review.md](reviews/phase7-code-review.md) | v1.19–v1.22 已提交改动复审及 v1.23 处理闭环；P1/P2 已处理，剩余 P3 登记 backlog |
| [reviews/phase8-code-review.md](reviews/phase8-code-review.md) | v1.23 提交区间复审及 v1.24 处理闭环；P1/P2 与 P3-1/P3-4 已处理，剩余 P3 登记 backlog |
| [reviews/phase9-code-review.md](reviews/phase9-code-review.md) | v1.25 现状全库四模块深审（补 v1.25 复审缺口）；口径项由 v1.26 关闭，第一、第二梯队、P3-34 与四组遗留清扫批均已归入 v1.27 完成 |
| [reviews/phase10-code-review.md](reviews/phase10-code-review.md) | v1.28–v1.31 四提交区间复审（助手编辑、加固批次、记忆系统、模型/提供商切换）；无 P0，3 P1 / 8 P2 / 23 P3；文档口径项（P3-6/P3-7/P3-23 与 P2-8 文档分支）由 v1.32 关闭，代码侧修复已在 v1.33 一次完成（P1 全部、P2 全部、P3 除 5 项观察项暂缓外全部），详见报告文末处理结果记录 |

## 历史设计

[history/superpowers/](history/superpowers/) 保存已完成功能的设计说明与实施计划。它们提供决策背景，不是现行接口或行为的依据；与架构文档冲突时始终以架构文档为准。
