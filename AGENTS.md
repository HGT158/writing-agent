# AGENTS.md — 项目约定与交接

## 项目与状态

个人写作 Agent（内容生产，非 Coding Agent）：Planner 每轮动态选择 Skill/工具，完成检索、归纳、大纲、成文、质检和归档，不是固定 Workflow。

- 架构单一事实来源：`docs/architecture/phase1-architecture.md` **v1.16**。
- 阶段 2、3、4、v1.13 项目 Agent 流式编辑、v1.14 空白文档生成、v1.15 项目 Agent 多会话历史及 v1.16 失败路径加固均已完成；当前基线：Python **162/162**、记忆隔离 **10/10**、前端 **43/43**。
- 阶段 4 已具备 FastAPI + SSE + Vue 3 写作 IDE、一助手多项目、选区改写、项目 Agent 流式编辑与每项目多会话历史。
- **阶段门：完成一个阶段后必须停下等待用户确认，不自动扩大范围。**

## 新会话必读

开始设计或代码工作前按顺序完整阅读：

1. `AGENTS.md`
2. `docs/architecture/phase1-architecture.md`
3. `README.md`

读完后先汇报当前理解、处理范围和验收方式；用户确认前不得开始下一阶段。

## 环境与验证

- Python 只能使用 `C:\miniconda\envs\writing-agent\python.exe`，禁止系统 Python、禁止新建 venv。
- pip 使用 `C:\miniconda\envs\writing-agent\python.exe -m pip ...`。
- 本机重建环境需设置 `CONDA_NO_PLUGINS=true` 并使用 `--solver=classic`。
- 无 API Key 冒烟测试设置 `MCP_SERVERS_JSON=config/mcp_servers.empty.json`。

```powershell
C:\miniconda\envs\writing-agent\python.exe -m pytest tests\test_memory_isolation.py -q -p no:cacheprovider --basetemp D:\test_agent\pytest-temp-writing-agent
C:\miniconda\envs\writing-agent\python.exe -m pytest tests -q -p no:cacheprovider --basetemp D:\test_agent\pytest-temp-writing-agent
Set-Location web
npm test
npm run typecheck
npm run build
```

## 代码边界

| 目录            | 责任                                       |
| ------------- | ---------------------------------------- |
| `agent/`      | 六节点 Agent Loop、Planner、Runtime、工具和 Skill |
| `memory/`     | 唯一持久化层；SQLite、记忆、项目、change set、运行锁       |
| `scheduler/`  | 与 Runtime 共用事件循环的 APScheduler            |
| `api/`        | FastAPI、SSE；只调用 MemoryStore/Runtime      |
| `web/`        | Vue 3 + TypeScript + CodeMirror 写作 IDE   |
| `mcp_client/` | 官方 MCP SDK stdio 客户端                     |

`memory/store.py` 是业务层唯一持久化门面。SQL 只能位于 `memory/`，`agent/`、`scheduler/`、`api/` 禁止直接执行 SQL。

## 硬性规则

1. 架构或跨模块契约变化必须先更新 `docs/architecture/phase1-architecture.md` 并升版。
2. 行为变更严格遵循 RED → GREEN → 全量回归；`tests/test_memory_isolation.py` 必须始终常绿。
3. 所有助手数据接口和查询必须以 `assistant_id` 隔离，不能只用 project/document/task id 授权。
4. 密钥只能来自 `.env`；禁止硬编码和提交。`.env.example` 只放占位配置。
5. 新增内置工具须在 `agent/tools.py` 注册，声明 `idempotent`/`captures_source`，写入受 `data/` 沙箱约束。
6. 只使用既有 `langgraph`、`langchain-core`、`langgraph-checkpoint-sqlite`，禁止引入 LangChain 全家桶。
7. 代码必须完整可运行，禁止伪代码、占位实现、吞异常或只改文档不改行为。
8. 保持改动聚焦，不撤销或覆盖用户工作；禁止擅自使用 `git reset --hard`、`git checkout --` 等破坏性命令。
9. 能从架构、代码和测试确定的事项自主推进，减少询问；缺少关键产品决策、授权或涉及不可逆操作时才提问。
10. Git 提交采用 Conventional Commits：`类型(可选范围): 中文摘要`；摘要和正文使用中文，技术名词可保留原文。
11. 提交或推送前检查 `.env`、`data/`、数据库、日志、依赖和构建产物未进入索引。
12. 完成工作后同步实际测试基线与相关文档，并遵守阶段门。
13. `docs/` 只保持 `architecture/`、`guides/`、`history/`、`reviews/` 四类目录；Superpower 产生的现行设计归入 `architecture/`，完成后的过程材料归入 `history/`，禁止新建 `docs/superpowers/` 等平行目录。

## 当前 Git 状态

- 仓库：`https://github.com/HGT158/writing-agent`（私有）。
- 默认分支：`main`，跟踪 `origin/main`。
- 用户要求当前文档精简结果经其审查后再提交；不得未经审查 commit/push。

## 已知暂缓项

- CLI 运行中按 Ctrl+C 可能显示 `KeyboardInterrupt` traceback，属于后续体验优化。
- 长短混合查询存在三字词元时只走 FTS，不额外为短词元增加 LIKE；这是架构 §5.7 的既定取舍。
