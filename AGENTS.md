# AGENTS.md — 项目约定与交接

## 项目与状态

个人写作 Agent（内容生产，非 Coding Agent）：Planner 每轮动态选择 Skill/工具，完成检索、归纳、大纲、成文、质检和归档，不是固定 Workflow。

- 架构单一事实来源：`docs/architecture/phase1-architecture.md` **v1.26**。
- 阶段 2、3、4 及 v1.13–v1.26 均已完成；当前基线：Python **228/228**、记忆隔离 **10/10**、前端 **117/117**。
- 阶段 4 已具备 FastAPI + SSE + Vue 3 写作 IDE、一助手多项目、选区改写、项目 Agent 流式编辑和每项目多会话历史。
- v1.17 补齐：活动 SSE 订阅者按 `seq` 跨越事件滑窗（修复长回复超过窗口后停流）、编辑器内联 diff + 侧栏卡片双视图、选区工具栏可输入、项目聊天上下文分层压缩、前端助手增删。
- v1.18 补齐：SSE 断线游标续传——数据帧带标准 `id: <seq>` 行，流端点接受 `after_seq` / `Last-Event-ID` 游标，游标落后于窗口时发 `reconnect_gap` 缺口信号；前端按退避自动重连、按 `seq` 去重，缺口后等待终态并重载持久化会话。
- v1.19 补齐：项目聊天持久化工作记录——`project_chat_work_events` 表、`work_item_start/delta/done` SSE 事件、done 时落库（单任务 199+1 条上限、参数/结果脱敏截断）、失败/取消以 interrupted 终结、会话详情对账补写终态、前端运行中展开终态折叠。
- v1.20 补齐：多 hunk change set 与逐 hunk 审查——`change_set_hunks` 拆表（单事务迁移）、编辑工具按文档分组提交 hunks（修复同文档多处修改整批失败缺陷）、接受单个 hunk 为唯一应用原语（同组内容复检、他组整组 stale）、hunk 级 API 与分页查询、前端每个 hunk 自带独立接受/放弃按钮（TRAE 式）。
- v1.21 补齐（phase6 复审）：聊天保留窗口总量兜底（单条首尾截断 + 窗口收缩，prompt 恒不超预算）、前端助手 id 规则对齐后端（含下划线）、待审卡片按 Agent 作用域项目过滤且资源管理器切换不再清空 pending、MCP 空变量日志降 debug、runtime assert 改显式 raise；暂不处理项见 `docs/guides/backlog.md`。
- v1.22 补齐：写作 IDE 多主题界面——语义 CSS 变量化 + 五套主题（纸墨/墨夜/暖卷/竹青/海湾），标题栏主题选择器，仅显式选择才写入 localStorage（未选择时随系统深浅实时联动），存储不可用降级默认主题，深色主题覆盖 CodeMirror。
- v1.23 补齐（phase7 复审）：工作记录脱敏整体修复（字符串参数先 json.loads 再 redact，不再绕过）、失败 detail 值级脱敏 + 截断、明细落库失败降级 warning 不打断任务、reject 前清理孤儿写意图（不再持续 409）、CodeMirror 语法高亮真正主题化（tagHighlighter 语义类映射，五套主题覆盖语法色）、连续对话保留上一轮已完成工作记录、侧栏卡片点击打开目标文档；accept-all 中断文案修复、backlog 补登记 phase6 暂缓项。P3 其余观察项见 `docs/guides/backlog.md`。
- v1.24 补齐（phase8 复审）：值级脱敏捕获组分支切片边界修复（key=value 长前缀形态曾完整泄漏敏感值，断言收紧为任意前缀子串不得出现）、明细落库失败降级 warning 补发配对 `work_item_start`（实时会话立即可见，SSE 配对序列有测）、hunk 定位回退（内联装饰不可用时按原文搜索定位，找不到给轻提示）；同版修复用户实测两缺陷：选区改写与项目聊天的 `change_preview` hunk 载荷补 `status` 字段（对齐 v1.20 契约，前端不再报"无效的修改预览"）、运行中工作记录耗时每秒自动跳动（响应式时间源）；另修复外部内容同步跳顶——文档身份 watcher 改多源形式按元素比较（数组 getter 恒触发曾致每次保存/接受 hunk 都销毁重建编辑器），内容同步只替换最小差异区间（不再整篇替换破坏滚动锚点）；再修复失效建议卡死侧栏——"全部放弃"此前只放弃 pending hunk、全 stale 卡片点不动，现 stale 一并放弃（服务端本就允许 stale→rejected），全失效卡片隐藏"全部接受"只留放弃/重试。其余 phase8 P3 观察项登记 backlog。
- v1.25 补齐：资源管理器树形嵌套渲染（项目文件树紧贴项目行、子文件夹可展开收起、同级名称交错排序）与项目/文件行内重命名删除——文档重命名（`PATCH .../documents/{id}`，路径校验、冲突/待处理建议/写意图拒绝、磁盘先行失败补偿）与文档删除（`DELETE .../documents/{id}`，入口文档改指）为新增后端契约；前端行内重命名为标签自身替换输入框（默认原名、聚焦并选中名称主体，Enter 提交 Esc 取消），删除需确认。
- v1.26 补齐（phase9 文档口径）：纯文档修正四处与实现的偏差，不改任何代码行为——任务终态记录按容量（128 条）有界保留（原误写「TTL/容量」）、fetch 结果口径修正（全文 ≤20,000 字符落 sources、≤500 字符摘要进 Observation，原误写「截断至 2000 字符进 Observation」）、文档重命名/删除的 mutation lock 明确以助手运行锁实现（助手任一任务运行期间 409）、项目聊天始终注入 editing 指导不受技能子集裁剪（选区改写仍校验）。phase9 审查报告见 `docs/reviews/phase9-code-review.md`。
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
- pytest 的 `--basetemp` 目录 `D:\test_agent\pytest-temp-writing-agent` 需事先存在；红线与全量两轮连跑时建议分别使用 `pytest-temp-isolation` / `pytest-temp-full` 两个目录——Windows 上 SQLite WAL 句柄未及时释放会让第二轮的 basetemp 清理偶发 PermissionError（受影响测试随机、单独跑恒绿），属环境竞态而非代码缺陷。
- 注：`pytest-temp-*` 临时目录由 pytest 每次自建自清，不应提交。

```powershell
C:\miniconda\envs\writing-agent\python.exe -m pytest tests\test_memory_isolation.py -q -p no:cacheprovider --basetemp D:\test_agent\pytest-temp-isolation
C:\miniconda\envs\writing-agent\python.exe -m pytest tests -q -p no:cacheprovider --basetemp D:\test_agent\pytest-temp-full
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
10. Git 提交遵循下方「提交信息规范」。
11. 提交或推送前检查 `.env`、`data/`、数据库、日志、依赖和构建产物未进入索引。
12. 完成工作后同步实际测试基线与相关文档，并遵守阶段门。
13. `docs/` 只保持 `architecture/`、`guides/`、`history/`、`reviews/` 四类目录；Superpower 产生的现行设计归入 `architecture/`，完成后的过程材料归入 `history/`，禁止新建 `docs/superpowers/` 等平行目录。

## 提交信息规范

格式：`<type>(<scope>): <subject>`

- type 仅允许：feat / fix / refactor / perf / docs / test / build / ci / chore / revert
- scope 使用业务模块名（如 agent / memory / api / web），可省略
- subject 简洁明确，不超过 72 个字符；不写「修改代码」「优化一下」「修复 bug」等无意义描述
- 一个 Commit 尽量只对应一个独立改动；修改复杂时再增加正文，简单修改只写一行
- 摘要与正文使用中文，技术名词可保留原文
- 不要根据描述猜测，必须以实际 Diff 为准——写提交信息前逐文件核对本次改动

## 当前 Git 状态

- 仓库：`https://github.com/HGT158/writing-agent`（私有）。
- 默认分支：`main`，跟踪 `origin/main`。
- 用户要求不得未经审查 commit/push。

## 已知暂缓项

- CLI 运行中按 Ctrl+C 可能显示 `KeyboardInterrupt` traceback，属于后续体验优化。
- 长短混合查询存在三字词元时只走 FTS，不额外为短词元增加 LIKE；这是架构 §5.7 的既定取舍。
- 上下文压缩的 token 估算是按字符类型折算的近似值，不接服务端计费口径；预算需要精确时再引入分词器。
- 编辑器内联 diff 在标签页 dirty 时撤下全部装饰；版本已推进的 hunk 按 `old_text` 内容唯一匹配重定位，匹配零次或多次（或属其他任务建议）时降级为侧栏卡片并提示失效。
- v1.20 已提供按文档分页的 change set 查询 API，前端打开文档与操作失败后会全量对账 hunk 级状态；SSE 缺口场景仍以重载持久化会话为主恢复路径。
