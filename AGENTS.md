# AGENTS.md — 项目约定与交接

## 项目与状态

个人写作 Agent（内容生产，非 Coding Agent）：Planner 每轮动态选择 Skill/工具，完成检索、归纳、大纲、成文、质检和归档，不是固定 Workflow。

- 架构单一事实来源：`docs/architecture/phase1-architecture.md` **v1.19**。
- 阶段 2、3、4 及 v1.13–v1.19 均已完成；当前基线：Python **199/199**、记忆隔离 **10/10**、前端 **73/73**。
- 阶段 4 已具备 FastAPI + SSE + Vue 3 写作 IDE、一助手多项目、选区改写、项目 Agent 流式编辑和每项目多会话历史。
- v1.17 补齐：活动 SSE 订阅者按 `seq` 跨越事件滑窗（修复长回复超过窗口后停流）、编辑器内联 diff + 侧栏卡片双视图、选区工具栏可输入、项目聊天上下文分层压缩、前端助手增删。
- v1.18 补齐：SSE 断线游标续传——数据帧带标准 `id: <seq>` 行，流端点接受 `after_seq` / `Last-Event-ID` 游标，游标落后于窗口时发 `reconnect_gap` 缺口信号；前端按退避自动重连、按 `seq` 去重，缺口后等待终态并重载持久化会话。
- v1.19 补齐：项目聊天持久化工作记录——`project_chat_work_events` 表、`work_item_start/delta/done` SSE 事件、done 时落库（单任务 199+1 条上限、参数/结果脱敏截断）、失败/取消以 interrupted 终结、会话详情对账补写终态、前端运行中展开终态折叠。
- **阶段门：完成一个阶段后必须停下等待用户确认，不自动扩大范围。**

## 新会话必读

开始设计或代码工作前按顺序完整阅读：

1. `AGENTS.md`
2. `docs/architecture/phase1-architecture.md`
3. `README.md`

读完后先汇报当前理解、处理范围和验收方式；用户确认前不得开始下一阶段。

## 环境与验证

- Python 只能使用 `C:\miniconda\envs\writing-agent\python.exe`（Python 3.13），禁止系统 Python、禁止新建 venv。
- pip 使用 `C:\miniconda\envs\writing-agent\python.exe -m pip ...`；依赖清单为 `requirements-dev.txt`（已包含 `requirements.txt`）。
- 包下载慢或超时：PyPI 优先走清华镜像（pip 加 `-i https://pypi.tuna.tsinghua.edu.cn/simple`，uv 用 `UV_DEFAULT_INDEX`），GitHub 等被墙资源走本地代理 `http://127.0.0.1:7890`（设置 `HTTP_PROXY`/`HTTPS_PROXY`）。本机 uvx 转发的子进程 stdio 不可靠，MCP server 一律用 conda 环境直跑。
- 本机重建环境需设置 `CONDA_NO_PLUGINS=true` 并使用 `--solver=classic`。
- Node.js 与 npm 已通过 `C:\nvm4w\nodejs` 加入 PATH，直接使用 `node` 和 `npm` 命令。
- 无 API Key 冒烟测试设置 `MCP_SERVERS_JSON=config/mcp_servers.empty.json`。
- pytest 的 `--basetemp` 目录 `D:\test_agent\pytest-temp-writing-agent` 需事先存在。

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

`agent/context.py` 负责项目聊天的 token 预算、历史压缩与文档窗口截断；Runtime 不内联裁剪逻辑。
`web/src/editor/` 存放 CodeMirror 扩展（内联 diff、选区持久高亮），组件不直接构造装饰。

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
- 上下文压缩的 token 估算是按字符类型折算的近似值，不接服务端计费口径；预算需要精确时再引入分词器。
- 编辑器内联 diff 只在标签页版本等于建议基准版本且无未保存修改时渲染，其余情况降级到侧栏卡片。
- 选区改写的 change set 目前只经 SSE 下发，流断线且出现缺口时前端无法凭查询接口找回漏发的建议，只能提示重新生成；统一查询对账 API 见 `docs/guides/backlog.md` 的多 hunk 设计。
