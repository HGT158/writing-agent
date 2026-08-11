# AGENTS.md — 项目约定与交接（供所有 Agent 会话遵守）

## 项目是什么

个人写作 Agent（内容生产，非 Coding Agent）：网络检索 → 素材归纳 → 大纲 → 分段成文 → 质检 → Markdown 归档。
核心判定标准：Planner 每轮**动态决定**调用哪些 Skill/工具（选择理由必填、全程可观测），不是固定 Workflow。

## 当前状态（2026-08-10）

- 阶段 1 架构：✅ 完成，文档 `docs/phase1-architecture.md` **v1.11**（阶段 4 复审加固契约已纳入）
- 阶段 2 MVP：✅ 完成，**pytest 24/24 全绿**（核心循环已有 FakeLLM 图级回归）
- 阶段 3（Memory 充实 + Scheduler）：✅ 完成，**pytest 50/50 全绿**
- 阶段 4（FastAPI + SSE + Vue 3 Web UI）：✅ 完成，**pytest 123/123、前端 30/30、记忆隔离 9/9 全绿**
- **铁律：每阶段完成后停下来等用户确认，再进下一阶段。**

## 新会话必读顺序

开始任何设计或代码工作前，按顺序完整阅读：

1. `AGENTS.md`：环境、硬性规则、当前基线与交接边界。
2. `docs/phase1-architecture.md`：架构 v1.11，项目单一事实来源。
3. `docs/phase3-code-review.md` 末尾“第二轮复审处理结果”：阶段 3 最终修复与验证证据。
4. `README.md`：用户可见的安装、运行、Scheduler 与 Windows Task Scheduler 操作。

读完后先向用户汇报：当前状态理解、拟处理范围、实施计划和验收方式。用户确认前不要开始下一阶段代码。

## Python 环境：一律使用 conda

本项目的所有 Python 操作（运行、测试、安装依赖）**必须使用 conda 环境 `writing-agent`**，
不要使用系统 Python，不要新建 venv，不要往其他环境装包。

- **解释器**：`C:\miniconda\envs\writing-agent\python.exe`
- **pip**：`C:\miniconda\envs\writing-agent\python.exe -m pip ...`

常用命令（在项目根目录 `writing-agent/` 下执行）：

```bash
# 运行 Agent
C:\miniconda\envs\writing-agent\python.exe -m agent run "任务" --assistant tech-writer

# 跑测试（改完代码必须全绿才算完成）
C:\miniconda\envs\writing-agent\python.exe -m pytest tests/ -v

# 装新依赖（先加进 requirements.txt，再安装）
C:\miniconda\envs\writing-agent\python.exe -m pip install -r requirements.txt
```

## 环境重建（仅当环境损坏时）

```bash
# 注意：本机 conda 的 libmamba 插件会报 "Upload did not complete" 并失败，
# 必须禁用插件并显式指定 classic solver：
CONDA_NO_PLUGINS=true C:\miniconda\Scripts\conda.exe create -n writing-agent python=3.13 -y --solver=classic
C:\miniconda\envs\writing-agent\python.exe -m pip install -r requirements.txt
```

## 代码地图

```
agent/          核心：loop.py（六节点状态机）/ planner.py / executor.py / runtime.py
                llm.py（json 模式回退）/ tools.py（沙箱内置工具）/ skills.py / schemas.py / events.py
mcp_client/     官方 SDK stdio；registry.py 支持 ${VAR}/${PROJECT_ROOT} 插值（超集扩展）
memory/         store.py（业务持久化门面，assistant_id 恒为第一参数）/ projects.py / short_term.py / long_term.py
api/            FastAPI 应用、项目/助手/任务/文章接口、SSE TaskBroker；只调用 MemoryStore/Runtime
web/            Vue 3 + TypeScript + Vite；CodeMirror 写作 IDE、项目树、选区工具栏、Agent 面板
skills/         research / writing / editing（SKILL.md frontmatter + tools.yaml）
config/         settings.py（.env 加载）/ mcp_servers.json
data/           运行时生成：app.db / checkpoints.db / assistants/ / articles/ / archive/
tests/          123 个 Python 测试；web/src 下 30 个前端测试
docs/           phase1-architecture.md（v1.11）+ 审查报告（含处理结果对照表）
```

`memory/store.py` 是业务层唯一可调用的持久化入口；具体 SQLite schema 与查询实现位于 `memory/` 内部，其他业务模块不得直接执行 SQL。

## 已实现能力基线

- **多助手**：AssistantRegistry 自动创建 `default`；persona、Skill 子集、长期画像和文章目录按助手隔离。
- **Agent Loop**：LangGraph 六节点 `LoadContext → Plan → Execute → Observe → Reflect → Finish`；Reflect 回 Observe，确保 `max_steps` 每轮生效。
- **记忆**：SQLite WAL + FTS5 trigram；messages/articles 外部内容索引、触发器同步、存量回填和不完整迁移重建均已实现。
- **检索降级**：无三字词元时使用字面量 OR LIKE；`\\`、`%`、`_` 转义；FTS 与 LIKE 都最多采样 16 项并覆盖首尾；profile/FTS/LIKE/最近文章各路异常只记 warning，不阻断写作。
- **跨任务延续**：第二次同主题任务可在真实 Runtime/Planner 链路读到第一次沉淀的偏好与文章索引。
- **并发控制**：`app.db` 的 `run_locks` 提供跨进程、按 assistant_id 的互斥，包含所有权校验和崩溃残留回收。
- **工具与 Skill**：内置文件工具受 `data/` 沙箱约束；MCP 使用官方 SDK stdio；Skill 渐进式披露并在激活前校验工具依赖。
- **Scheduler**：Runtime 持有 AsyncIOScheduler，共用当前 asyncio 事件循环；job 绑定 assistant_id，默认 job 使用 `default`；助手正忙时跳过并 warning。
- **CLI 生命周期**：Runtime 启动与执行都在清理边界内，普通异常转 failed 事件和非零退出码，资源始终关闭。
- **文章项目**：一助手多项目；空白项目、文本/文件夹复制导入、项目树、多标签编辑、乐观版本保存、归档与显式 purge 已实现。
- **AI 修改**：选区改写和项目 Agent 聊天复用 AgentRuntime、editing Skill、EventBus 与运行锁；只生成 change set/diff，接受后才原子写入。
- **Web/API**：FastAPI + SSE + Vue 3 写作 IDE；桌面三栏，窄屏项目/Agent 互斥抽屉；完成态文章只读 API 与助手/普通任务 API 已实现。

## 硬性约定

1. **架构改动先改文档**：`docs/phase1-architecture.md` 是单一事实来源，代码与文档不一致时以文档为准升版。
2. **红线测试必须常绿**：`tests/test_memory_isolation.py`（多助手记忆隔离）。任何 store/锁改动后先跑红线，再跑全量测试。
3. **业务代码禁止裸写 SQL**：`agent/`、`scheduler/`、未来 `api/` 等模块只能调用 `MemoryStore`；SQL 仅允许位于 `memory/` 层。新表和所有助手所有权查询必须含 `assistant_id`，并补隔离测试。
4. **密钥只走 `.env`**（从 `.env.example` 复制），禁止硬编码、禁止提交（.gitignore 已配）。
5. **无 API Key 的冒烟测试**：`MCP_SERVERS_JSON=config/mcp_servers.empty.json` 跳过 MCP 启动。
6. **新增内置工具**：在 `agent/tools.py` 注册并声明 `idempotent`/`captures_source`；沙箱限 `data/`。
7. **不要引入 LangChain 全家桶**：只用 langgraph(>=1.0) + langgraph-checkpoint-sqlite(>=3.0)（架构假设 A5）。
8. **外部前置依赖**：Node（npx，Tavily MCP）与 uvx（fetch MCP，requirements 中的 uv 提供）。
9. **代码必须完整可运行**：禁止伪代码、占位实现、吞异常或只改文档不改行为；行为变更遵循 RED → GREEN → 全量回归。
10. **保护现有工作区**：项目已初始化 Git 仓库，默认分支为 `main`，但尚无首次提交；在建立基线提交前不要依赖 `git diff` 判断全部改动，禁止擅自使用 checkout/reset 撤销内容，也不要删除或覆盖不属于当前任务的文件。
11. **阶段内默认自主推进**：不要频繁向用户询问或逐项等待确认。能从架构、代码、测试和现有约定中确定的事项，由 Agent 做保守合理的决定并连续完成设计、实现与验证；进度用简短更新说明。只有缺失信息会实质改变产品方向、需要新增授权、涉及不可逆操作或现有约定无法消解冲突时才提问。每个完整阶段结束后仍按阶段门停下等待用户确认。
12. **Git 提交信息使用中文规范格式**：采用 Conventional Commits，格式为 `类型(可选范围): 中文摘要`，例如 `feat(web): 增加选区改写工具栏`、`fix(memory): 修复写入意图恢复竞态`。类型使用 `feat`、`fix`、`docs`、`test`、`refactor`、`chore` 等约定前缀；摘要和提交正文使用中文，技术名词可保留原文。

## 标准修改与验证流程

1. 先核对 `docs/phase1-architecture.md`；架构或跨模块契约变化先升版并写明变更。
2. 为行为变化补最小失败测试，使用指定 conda Python 观察预期 RED。
3. 实现最小修复，跑目标测试转 GREEN。
4. Memory/锁相关改动先跑红线，再跑完整测试：

```powershell
C:\miniconda\envs\writing-agent\python.exe -m pytest tests/test_memory_isolation.py -v -p no:cacheprovider --basetemp D:\test_agent\pytest-temp-writing-agent
C:\miniconda\envs\writing-agent\python.exe -m pytest tests/ -v -p no:cacheprovider --basetemp D:\test_agent\pytest-temp-writing-agent
```

5. 用实际测试数同步 `AGENTS.md`、架构状态、README 和对应审查文档处理结果。
6. 完成本阶段后停下汇报，等待用户明确确认，不自动进入下一阶段。

## 阶段 3 任务与验收结果

1. ✅ `memory/store.py` 的 `recall` 已升级为 **FTS5 trigram** 检索（无三字词元时回退有界、转义后的 LIKE，接口签名不变）；第二次同主题调用真实 Planner 时可读到第一次沉淀的偏好与文章。
2. ✅ **APScheduler** 已挂到 Runtime 同一 asyncio 事件循环，`config/settings.py` 的 `JOBS` 生效（每个 job 绑定 assistant_id）；cron 任务可注册、到期派发，助手正忙时跳过并记 warning。
3. ✅ README 已附 Windows Task Scheduler XML 导入、导出与开机自启说明。
4. ✅ 当前完整回归已补齐；指定 conda 环境下 `pytest tests/ -v` **123/123 全绿**。

## 阶段 4 任务与验收结果

- ✅ FastAPI 本地 API、SSE TaskBroker 和 Vue 3/Vite 前端已完成，生产构建由 FastAPI 托管 `web/dist`。
- ✅ 一助手多项目、空白项目、文本/文件夹复制导入、项目资源树、CodeMirror 多标签编辑和 Markdown 预览已完成。
- ✅ 选区改写工具栏与项目 Agent 聊天均生成 change set/diff；接受/拒绝、版本冲突、原文快照和助手归属校验已完成。
- ✅ 项目归档会阻止运行中任务或待处理 change set；显式 purge 清除文件与元数据；助手 purge 同步清除项目表和项目归档。
- ✅ 浏览器实测覆盖桌面三栏、390px 窄屏互斥抽屉、项目创建、编辑保存和选区工具栏。
- ✅ 最终基线：Python **123/123**、记忆隔离 **9/9**、前端 **30/30**，`vue-tsc` 与 Vite 生产构建通过，`agent/`、`scheduler/`、`api/` SQL 边界扫描为空。
- **阶段门**：阶段 4 已完成，必须停下等待用户确认；不得自动进入下一阶段或扩大产品范围。

## 已知暂缓项

- `python -m agent run` 执行中按 Ctrl+C 可能显示 KeyboardInterrupt traceback；这是阶段 2 遗留的 CLI 体验项，后续如需优化应单独立项，不属于阶段 4 回归。
- 长短混合查询只要存在三字词元就走 FTS，短词元不额外加入 LIKE；这是架构 §5.7 的明确取舍，不应当作缺陷擅自修改。
- 项目已初始化 Git 仓库（`main`，尚无提交）；执行首次 `git add`/commit、配置远程仓库或制定分支策略前必须由用户单独确认。
