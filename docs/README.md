# 文档导航

本目录按“现行依据优先、审查记录可追溯、历史设计不混入现行规范”整理。运行时架构与跨模块契约只以架构文档为准。

## 现行依据

| 文档 | 用途 |
|---|---|
| [architecture/phase1-architecture.md](architecture/phase1-architecture.md) | 架构单一事实来源（v1.16） |
| [architecture/project-agent-streaming-edit-design.md](architecture/project-agent-streaming-edit-design.md) | 项目 Agent 流式编辑工具设计（已实现） |
| [architecture/project-chat-history-design.md](architecture/project-chat-history-design.md) | 项目 Agent 多会话历史设计（已实现） |
| [../AGENTS.md](../AGENTS.md) | Agent 工作约定、环境和硬性规则 |
| [guides/new-session-prompt.md](guides/new-session-prompt.md) | 新对话的可复制启动提示词 |
| [guides/windows-task-scheduler.md](guides/windows-task-scheduler.md) | Windows 登录启动 Scheduler 的配置 |
| [guides/backlog.md](guides/backlog.md) | 已确认但暂缓实施的后续能力 |

## 审查记录

审查报告按阶段保留，用于追溯问题、修复和当时的验证结果。报告中的版本号与测试数量是历史事实，不会因后续阶段完成而回写。

| 文档 | 范围 |
|---|---|
| [architecture/phase1-architecture-review.md](architecture/phase1-architecture-review.md) | 阶段 1 架构审查 |
| [reviews/phase2-code-review.md](reviews/phase2-code-review.md) | 阶段 2 MVP 代码审查 |
| [reviews/phase3-code-review.md](reviews/phase3-code-review.md) | 阶段 3 Memory + Scheduler 审查与二轮复审闭环 |
| [reviews/phase4-code-review.md](reviews/phase4-code-review.md) | 阶段 4 写作 IDE 审查与处理结果 |
| [reviews/phase5-code-review.md](reviews/phase5-code-review.md) | 项目 Agent 流式编辑与多会话增量审查及处理结果 |

## 历史设计

[history/superpowers/](history/superpowers/) 保存已完成功能的设计说明与实施计划。它们提供决策背景，不是现行接口或行为的依据；与架构文档冲突时始终以架构文档为准。
