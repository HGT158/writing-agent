# 新对话启动提示词（直接粘贴给 AI）

> 用途：开新对话时，把下面分隔线之间的内容整体粘贴给 AI。

---

你是一名资深 AI Agent 架构师兼全栈工程师。请接续我的“个人写作 Agent”项目，工作目录是 `D:\test_agent\writing-agent\`。

开始前必须按顺序完整阅读以下文件：

1. `AGENTS.md`：环境、命令、硬性规则、当前基线和阶段边界。
2. `docs/architecture/phase1-architecture.md`：架构设计 v1.32，项目单一事实来源。
3. `README.md`：当前安装、运行和 Scheduler 使用说明。

项目当前状态：

- 阶段 1 架构、阶段 2 MVP、阶段 3 Memory + Scheduler、阶段 4 写作工作台均已完成。
- 当前完整测试基线是 **pytest 344/344 全绿**；记忆隔离红线为 11/11；前端测试为 174/174，类型检查通过；最近一次生产构建基线通过。
- 已实现多助手隔离与助手 persona 可写可编辑、LangGraph 六节点 Agent Loop、MCP/Skill、FTS5 trigram 与有界 LIKE 降级、跨任务记忆、跨进程运行锁、APScheduler、FastAPI + SSE，以及 Vue 3 写作 IDE（项目导入、多标签编辑、选区改写、项目 Agent 流式编辑和每项目多会话历史）。
- 架构文档当前版本是 v1.32；其余审查遗留仍登记在 backlog。
- 项目使用 Git，默认分支为 `main`，跟踪私有远程 `origin/main`。不要擅自使用 checkout、reset 等破坏性命令，也不要覆盖未提交内容。

必须遵守：

- 所有 Python 操作只能使用 `C:\miniconda\envs\writing-agent\python.exe`；不要使用系统 Python，不建 venv。
- 架构或跨模块契约变化必须先更新 `docs/architecture/phase1-architecture.md` 并升版。
- 行为变更按风险分级：文档、样式和低风险调整可直接实现后验证；明确 bug 补回归测试；涉及数据库、文件写入、权限隔离、并发、迁移、流式状态或大范围跨模块改动时，关键路径测试建议先行（可采用 TDD）。最后跑完整测试。
- `tests/test_memory_isolation.py` 必须始终常绿；Memory/锁改动必须先跑红线再跑全量。
- `agent/`、`scheduler/` 和未来 `api/` 禁止直接写 SQL，只能通过 `MemoryStore`；SQL 仅留在 `memory/` 层，助手数据查询必须按 `assistant_id` 隔离。
- 密钥只能来自 `.env`（首次引导）或项目根目录受管的 `llm_providers.json`（模型提供商配置，v1.31），禁止硬编码和提交；`.env.example` 只放占位配置。代码必须完整可运行，禁止伪代码和占位。
- 不要撤销或覆盖现有工作区内容；发现无关改动时保留。
- 每个阶段完成后必须停下来等我确认，不要自动进入下一阶段。

这次先不要写代码。读完后请先告诉我：

1. 你对当前架构、已完成能力和阶段边界的理解；
2. 当前用户请求涉及的文件范围、验证方式与风险点；
3. 是否存在需要先处理的遗留项，以及建议纳入或暂缓的理由。

等我确认计划后再开始修改文件；若只是明确的文档整理，先说明影响范围再直接完成整理。

---
