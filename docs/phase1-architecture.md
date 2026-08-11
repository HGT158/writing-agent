# 个人写作 Agent — 阶段 1：架构设计文档

> 版本：v1.11 · 2026-08-10
> 状态：阶段 4 写作 IDE 及 v1.11 复审加固已完成（Python 123/123、前端 30/30、记忆隔离 9/9），文档与代码已同步
> v1.1 变更：新增「Assistant（助手）」一等概念——多助手、助手间记忆隔离、同助手跨会话记忆共享（见第 4 节）
> v1.2 变更：根据《阶段 1 架构文档审查报告》修复全部 4 个 P0、6 个 P1、12 个 P2 问题。主要改动：Planner 降级路径可路由化（§5.1/§9）、状态图与路由描述对齐（§3）、文章 API 隔离红线收紧（§5.9）、新增同助手并发控制（§4.6）、内置文件工具沙箱化并移除默认 filesystem MCP（§5.6）、Skill 依赖缺失边界（§5.5）、上下文裁剪策略（§3.3）、Reflect 质检清单（§3.4）、助手删除语义（§4.2）、中文检索定案 FTS5 trigram（§5.7）、tests 纳入 MVP（§8/§10）及一批 P2 措辞修正。
> v1.3 变更：根据复审意见修复 R1–R5——运行锁改为 app.db 内 `run_locks` 表实现**跨进程互斥**并定义崩溃残留回收（§4.6）；补 `sources` 表定义（含 `assistant_id` 列，§5.7）；trigram 不足 3 字查询回退 LIKE（§5.7）；统一工具协议增加隐式 `ToolContext` 注入 `assistant_id`（§5.2）；`AgentState` 补 `reflect_fails` 计数字段（§3.1）；删除助手前检查运行锁（§4.2）。
> v1.4 变更：与阶段 2 代码审查修复同步——**Reflect 回边由"回 Plan"改为"回 Observe"**（§3 状态图与路由描述），保证每轮循环经过计数节点、max_steps 真正生效；运行锁占位原子化（INSERT OR IGNORE）+ 释放带 task_id 所有权（§4.6）；成文素材改由 `sources` 表回读注入（§5.7）；`AgentState` 移除死字段 `sources`。其余代码层修复详见《阶段 2 代码审查报告》处理结果。
> v1.5 变更：明确阶段 3 实施约定——`messages` / `articles` 使用 FTS5 trigram 外部内容索引、触发器同步与存量数据回填，不足 3 字查询回退 LIKE（§5.7）；Runtime 持有 `AsyncIOScheduler`，只在 `python -m agent schedule` 长驻模式启用，与 Runtime 共用当前 asyncio 事件循环，普通 `run` 仍是一次性执行（§5.4/§5.8）。
> v1.6 变更：与阶段 3 代码审查修复同步——短词元查询按词元降级为转义后的参数化 LIKE，长查询在全体候选词元中均匀采样；profile 或 SQLite 任一路 recall 失败仅 warning 并独立降级，不阻断写作任务（§5.7/§9）；内置 JOB 改绑自动创建的 `default` 助手并明确 60 秒 misfire 规则（§5.8）；CLI 将 Runtime 启动纳入清理边界，启动或运行异常均关闭已分配资源（§5.4/§9）。
> v1.7 变更：根据阶段 3 第二轮复审 R1 收紧 LIKE 降级查询——模式数量与 FTS 查询一致最多 16 个，超限时在全部短词元中均匀采样并保留首尾，避免极端短词任务生成无上限 OR LIKE 全表扫描（§5.7）。
> v1.8 变更：阶段 4 改为“文档优先”的写作工作台；增加可继续编辑的文章草稿、单调递增文档版本和选区局部改写建议。局部改写必须复用 AgentRuntime/助手隔离/运行锁，先生成预览再显式应用，并以 `assistant_id`、`article_id` 与 `document_version` 做归属和乐观并发校验（§4.7/§5.7/§5.9/§5.10）。
> v1.9 变更：阶段 4 的文档模型扩展为“一助手多文章项目”。一个项目对应助手受管目录下的一个文件夹；导入文件夹时完整复制目录树，导入 `.md`/`.markdown`/`.txt` 时自动创建同名项目文件夹。Web UI 采用 VS Code 式项目资源管理器、多标签编辑器与右侧 Agent 面板；选区改写和聊天修改统一生成可审阅 change set，确认后才写入项目文件（§4.7/§5.7/§5.9/§5.10）。
> v1.10 变更：根据阶段 4 代码审查收紧写作 IDE 契约——文档保存与 change set 应用采用跨进程串行、可恢复的写入意图，冲突请求不得触碰正文文件；一次聊天产生的多个 change set 必须先全量校验再原子落库；任务记录携带 `assistant_id`、SSE 按订阅者广播且终态记录有界保留；内置写工具禁止覆盖受管项目；前端预览必须消毒，所有保存/应用操作以标签页或 change set 自身 `project_id` 为准，并保护 dirty 内容、同步 CodeMirror 与关闭 SSE（§4.7/§5.7/§5.9/§5.10/§6.2）。
>
> v1.11 变更：根据阶段 4 复审补齐恢复与异步作用域契约——运行锁和文档写入意图记录 PID 对应的进程启动时间，遗留旧行以创建时间 TTL 兜底，PID 复用不得造成永久阻塞；写入意图先用短事务认领、在事务外原子写文件、再用短事务终结，认领/插入竞态稳定映射 HTTP 409；MemoryStore 启动时对账 `.import-*`、`.purge-*` 与归档半程残骸，约束表迁移必须原子回滚；导入文件的 UTF-8 BOM 在后续保存/apply/恢复中保持；资源冲突使用专用异常；所有异步任务端点在返回 202 前校验助手与运行锁；前端的助手切换、项目树、文档打开、rewrite、apply/reject 和 SSE 只接纳发起作用域仍匹配的异步响应，待审卡片仅在父级确认 apply/reject 成功后移除（§4.7/§5.7/§5.9/§5.10/§6.2/§9）。

---

## 0. 设计假设

| # | 假设 | 影响 |
|---|------|------|
| A1 | 单用户、本地运行，无鉴权 | FastAPI 只绑 `127.0.0.1`；SQLite 单文件 |
| A2 | LLM 走 OpenAI 兼容接口，默认 DeepSeek | 一套 client 代码，`base_url` 切换服务商 |
| A3 | 搜索首选 Tavily MCP Server，Brave 备选 | `mcp_servers.json` 配置切换，代码无关 |
| A4 | 中文写作为主，长文分段生成 | 大纲→逐节成文→合并，避免单次输出截断 |
| A5 | LangChain 系只引入 `langgraph` + `langchain-core` + `langgraph-checkpoint-sqlite`（含 aiosqlite），不引入全家桶其余部分 | 依赖轻，工具抽象层自己控制 |
| A6 | **多助手 ≠ 多用户**：所有助手同属一个用户，隔离只为上下文纯净，不做权限 | 隔离靠命名空间约定（`assistant_id` 贯穿所有表和目录），无鉴权层 |

---

## 1. 核心设计决策（先看结论）

| 决策 | 结论 | 一句话理由 |
|------|------|-----------|
| Agent vs Workflow | **Planner 每轮动态决策**，而非固定 DAG | 满足"根据任务动态决定调用哪些 Skill/Tool"的核心判定标准 |
| Agent Loop 实现 | LangGraph `StateGraph` + 条件边 | 状态机显式建模"观察→规划→执行→反思"循环，自带流式事件与 checkpoint |
| Planner 输出 | **强类型 Pydantic 模型**（子任务 + 工具/Skill 选择 + 选择理由）；**降级兜底也必须可路由**（强制 finish 或 failed） | 理由可观测；兜底不产生无法驱动状态机的中间态 |
| 工具协议 | 统一 `ToolSpec` 抽象，内置工具与 MCP 工具**同一张表**；内置文件工具**沙箱限制在 `data/` 内** | Agent 核心对工具来源无感知；文件写入不越界 |
| Skill 机制 | 对齐 Claude Code：`SKILL.md`（YAML frontmatter + 正文 prompt），**渐进式披露**；激活前校验依赖工具可用性 | 规划时只注入元数据（省 token）；依赖缺失时激活失败可见、可决策 |
| MCP | 官方 `mcp` Python SDK，stdio 传输，配置文件注册 | 严格遵循官方规范，不自造协议 |
| **多助手与记忆隔离** | **Assistant 为一等概念，`assistant_id` 是所有持久化数据的命名空间** | 助手间零共享（记忆/文章/会话），同助手跨会话全共享 |
| **并发控制** | **同一 `assistant_id` 同时只允许一个运行中任务**，后来者拒绝（API 409 / CLI 报错）；锁落 app.db 的 `run_locks` 表，**跨进程有效** | CLI/FastAPI/多 CLI 窗口是多进程，内存锁互不可见；INSERT 即获锁 + 过期回收，成本与内存锁相当 |
| 记忆 | SQLite（会话/消息/文章索引）+ Markdown（长期画像），**均按助手分命名空间**；**中文检索定案 FTS5 trigram** | 文件可读可改，DB 可查可索引；trigram 对中文子串匹配有效 |
| 流式 | LangGraph `astream_events` → 事件总线 → SSE | 思考过程、工具调用、正文 token 三类事件统一推送 |
| **文档优先交互** | **文章项目/文件是 Web 主对象，聊天是对当前项目的 Agent 操作入口** | 让 Agent 服务写作产物，而不是复刻通用角色聊天界面 |
| **选区局部改写** | 选中文本 → 指令 → AI 建议 → diff 预览 → 用户确认应用 | 只改变用户明确选中的范围，避免整篇文章被意外覆盖 |

---

## 2. 总体架构

```mermaid
flowchart TB
    subgraph IF["接口层"]
        CLI["CLI (python -m agent --assistant X)"]
        WEB["Vue 3 SPA（助手选择器）"]
        API["FastAPI + SSE"]
        CLI --> RUNTIME
        WEB -->|HTTP / SSE| API --> RUNTIME
    end

    subgraph RUNTIME["Agent Runtime (LangGraph StateGraph)"]
        LOOP["Agent Loop<br/>observe → plan → act → reflect"]
        ASST["Assistant Registry<br/>加载助手配置/人设/技能子集"]
        LOCK["Per-Assistant Lock<br/>run_locks 表·跨进程"]
        SKILLMGR["Skill Manager<br/>扫描 / 注册 / 依赖校验 / 按需注入"]
        TOOLREG["Unified Tool Registry<br/>内置工具(沙箱) + MCP 工具同协议"]
        LOCK --> LOOP
        LOOP --> ASST --> SKILLMGR
        LOOP --> TOOLREG
    end

    subgraph MCPL["MCP 层 (官方 SDK · stdio) —— 全局共享的基础设施"]
        MCPCLIENT["MCP Client<br/>启动时发现 tools"]
        SEARCH["tavily / brave search"]
        FETCH["fetch server"]
        MCPCLIENT --> SEARCH & FETCH
    end

    subgraph MEM["Memory 层 —— 按 assistant_id 分命名空间"]
        ST["短期：SQLite sessions/messages<br/>(assistant_id 列, WAL 模式)"]
        LT["长期：assistants/&lt;id&gt;/memory/profile.md<br/>+ articles 索引表(FTS5 trigram)"]
    end

    SCHED["Scheduler (APScheduler)<br/>每个 job 绑定一个 assistant_id"] --> RUNTIME
    TOOLREG --> MCPCLIENT
    RUNTIME --> MEM
    RUNTIME -->|产出| OUT["data/articles/&lt;assistant_id&gt;/*.md"]
    RUNTIME -->|导入/编辑| PROJECTS["data/assistants/&lt;assistant_id&gt;/projects/&lt;project_id&gt;/"]
```

**分层原则**：接口层不含业务逻辑；Runtime 不知道工具来自 MCP 还是内置；MCP 层不知道 Skill 存在；Memory 对 Planner 只暴露两个方法（`recall` / `memorize`），且**所有调用必须携带 `assistant_id`**。

**共享 vs 隔离的边界**（重要）：

| 共享（全局基础设施，非记忆） | 隔离（每助手独立命名空间） |
|------|------|
| MCP 工具连接、Skill 目录（能力本身）、LLM client、Scheduler 进程 | 长期画像 profile.md、文章索引、会话/消息历史、文章输出目录、文章项目目录、运行锁 |

---

## 3. Agent Loop 状态机（核心）

```mermaid
stateDiagram-v2
    [*] --> Observe
    Observe --> Plan : 汇总上下文+本助手记忆
    Plan --> Act : next_action = call_tool / activate_skill
    Plan --> Write : next_action = write
    Plan --> Done : next_action = finish（含降级兜底）
    Act --> Reflect : 工具/Skill 执行完毕
    Reflect --> Observe : 素材不足或质检未过，回到循环入口（step+1）
    Write --> Reflect : 草稿完成，质检
    Reflect --> Done : 达标或超 max_steps
    Done --> [*]
```

> 路由规则（图文一致）：**条件边挂在 Plan 与 Reflect 两个节点之后**。Plan 之后按 `state.plan.next_action` 四路分发（Act/Write/Done；`activate_skill` 走 Act 边完成注入）；Reflect 之后按质检结果二路分发（回 Observe 或 Done）。
>
> **为什么回边指向 Observe 而非 Plan**（v1.4 修正）：`step` 只在 Observe 递增，若 Reflect 直接回 Plan，计数器整个任务生命周期只执行一次，`max_steps` 防死循环与 Planner 进度提示会整体失效——这是阶段 2 代码审查发现的 P0 级缺陷，回边必须让每轮循环都经过计数节点。

### 3.1 状态定义（AgentState）

LangGraph 的 State 用 `TypedDict`，全链路单一数据源：

```python
class AgentState(TypedDict, total=False):
    assistant_id: str              # ★ 当前助手，整个 Loop 不可变
    task: str                      # 用户原始目标
    session_id: str                # CLI 每次运行生成 uuid4；--resume <session_id> 从 checkpoint 续接
    memory_context: str            # 任务启动时 recall 一次的结果，Planner 每轮从此注入
    messages: list[BaseMessage]    # 对话/工具消息（add_messages reducer；裁剪策略见 §3.3）
    plan: PlanModel | None         # Planner 当前计划（Pydantic）
    active_skills: list[str]       # 已激活的 Skill 名
    observations: list[Observation]# 工具执行结果的结构化摘要（非原始全文）
    draft: str                     # 当前草稿（Markdown）
    step: int                      # 已用步数（防死循环）
    reflect_fails: int             # 连续质检未过次数（≥3 强制 finish，见 §3.4）
    status: str                    # running / done / failed
    output_path: str | None        # 最终文件路径
```

### 3.2 与"固定 Workflow"的本质区别

- **路由由 Planner 节点决定**：条件边读取 `state.plan.next_action`（`call_tool` / `activate_skill` / `write` / `finish`），而不是写死 research→write 的顺序。
- **Skill 选择发生在运行时**：Planner 每轮都能看到 Skill 元数据清单，简单任务（如"润色这段文字"）可以跳过 research 直接激活 editing。
- **循环可回退**：Reflect 节点判定素材不足时，可以带着新查询词回到 Plan，再次调用搜索——固定 DAG 做不到这一点。
- **max_steps 兜底**：默认 25 步，超限强制收敛到"基于已有素材成文"，防止 token 燃烧。

### 3.3 上下文裁剪策略（防 token 爆炸）

`max_steps` 只防死循环，不防上下文膨胀。裁剪规则：

1. **工具原始输出不进 `messages`**：Executor 把原始结果压缩为 `Observation`（成功：`summary` ≤500 字 + 关键字段；失败：`error` 信息），只有 Observation 进入对话历史。fetch 的网页全文截断至 2000 字符后进 Observation，全文落 SQLite `sources` 表备查。
2. **观察滑窗**：Planner 每轮只看最近 8 条 Observation 全文；更早的压缩为一行索引（`[3] tavily_search("模型蒸馏") → 5 条结果`）。
3. **强制压缩**：估算 prompt token 超阈值（默认 60k，可配）时，用一次 LLM 调用把滑窗外的观察总结成一段，替换原始条目。
4. **正文不进循环上下文**：分段写作时，已完成的章节存 `state.draft` / 文件，下一节只带大纲和本章要点，不带全文。

### 3.4 Reflect 质检清单（防自由心证）

Reflect 节点的 prompt 内置明确 checklist，逐项判定后才允许 `done`：

- 引用来源 ≥3 且每条可追溯（URL + 抓取时间）
- 大纲每个章节均有素材覆盖，无空节
- 正文包含来源标注
- 字数达到任务要求下限
- 关键数字/人名/日期已在来源中出现（不臆造）

任一项不过 → 回 Plan 并携带缺失项说明；连续 3 次质检未过 → 强制 finish 并在文末标注存疑项。

---

## 4. Assistant 模型与记忆隔离

### 4.1 什么是 Assistant

Assistant = **有独立身份、人设提示词、技能子集和独立记忆空间的写作助手实例**。例如「科技作者」擅长深度技术长文、「营销文案」偏好短平快风格——两者各写各的，互不污染对方的风格记忆。

### 4.2 助手定义（文件即配置）

```
data/assistants/
  tech-writer/
    assistant.yaml     # 助手定义
    persona.md         # 人设/系统提示词（可选，内容注入 system prompt）
    memory/profile.md  # 长期记忆（Agent 自维护，人可手改）
  marketing/
    ...
  default/             # 内置兜底助手，CLI 不指定 --assistant 时使用
```

`assistant.yaml`：

```yaml
id: tech-writer
name: 科技作者
description: 深度技术文章写作，注重引用来源与逻辑严密
skills: [research, writing, editing]   # 可裁剪的技能子集；缺省 = 全部
persona_file: persona.md               # 可选
created_at: "2026-08-05"
```

**为什么是文件而不是数据库**：助手定义是"配置"不是"状态"，人应该能用记事本直接增删改——符合白盒原则。运行时由 `AssistantRegistry` 扫描 `data/assistants/` 加载，新增文件夹即新增助手，无需重启之外的操作。

**删除语义（默认归档，非抹除）**：
- **删除前置检查**：先查 `run_locks` 表（§4.6）——该助手有运行中任务时**拒绝删除**并提示"任务运行中，请先等待完成"（Windows 上移动被进程占用文件的目录会直接失败，必须先挡）。
- 删除助手（CLI `assistants delete <id>` / API `DELETE`）：整个目录移动到 `data/archive/<id>-<时间戳>/`；SQLite 中该助手的 `sessions` / `messages` / `articles` 行**保留**，但因助手未注册而查询不到（等同不可见）。
- 只有显式加 `--purge` 才级联删除 SQL 行与归档目录。
- 恢复 = 把目录从 archive 移回 `data/assistants/`，SQL 数据自动重新可见。

### 4.3 隔离规则（核心）

| 数据 | 载体 | 命名空间方式 | 同助手跨会话 | 跨助手 |
|------|------|------------|:---:|:---:|
| 长期画像（风格/偏好/常用主题） | `assistants/<id>/memory/profile.md` | 目录隔离 | ✅ 共享 | ❌ 物理隔离 |
| 会话与消息 | SQLite `sessions` / `messages` 表 | `assistant_id` 列，**所有查询强制 WHERE** | ✅ 可检索历史会话 | ❌ 查询不到 |
| 文章索引 | SQLite `articles` 表 | `assistant_id` 列 | ✅ 共享 | ❌ |
| 文章文件 | `data/articles/<assistant_id>/` | 目录隔离 | ✅ | ❌ |
| 文章项目 | `data/assistants/<id>/projects/<project_id>/` + SQLite 项目/文件元数据 | 助手目录物理隔离 + `assistant_id` 强制过滤 | ✅ | ❌ |
| Agent Loop checkpoint | SQLite | `thread_id = <assistant_id>:<session_id>` | — | ❌ |
| MCP 工具 / Skill 目录 / LLM client | 进程级 | 不隔离（是能力不是记忆） | ✅ | ✅ |

**强制手段**：`memory/store.py` 的接口签名把 `assistant_id` 作为**第一个必填位置参数**（`recall(assistant_id, query)` / `memorize(assistant_id, ...)`），从类型层面杜绝"忘了过滤"的串记忆事故；SQL 全部走 store 层，业务代码禁止裸写 SQL。

### 4.4 记忆隔离数据流

```mermaid
flowchart LR
    subgraph A1["助手 tech-writer"]
        S1["会话 A"] & S2["会话 B"] --> R1["recall(tech-writer, ...)"]
        R1 --> P1["profile.md<br/>+ 历史文章索引"]
    end
    subgraph A2["助手 marketing"]
        S3["会话 C"] --> R2["recall(marketing, ...)"]
        R2 --> P2["profile.md<br/>+ 历史文章索引"]
    end
    P1 -.->|互不可见| P2
```

- 同助手：会话 B 开始时，`recall` 能拿到会话 A 沉淀的偏好与文章索引——**"第二次写同主题文章能引用偏好"由此保证**。
- 跨助手：marketing 的 `recall` 物理上够不到 tech-writer 的 profile（不同文件）和索引（SQL 强制过滤）。

### 4.5 对既有模块的影响

| 模块 | 变更 |
|------|------|
| Runtime | `run(assistant_id, task)`；启动时 `AssistantRegistry` 加载全部助手，运行时按 id 取人设/技能子集组装 system prompt；**按助手粒度加运行锁（§4.6）** |
| Planner | 注入的记忆摘要 = `state.memory_context`（启动时 `recall(assistant_id, task)` 一次）；看到的 Skill 清单 = 助手技能子集 ∩ 全局 skills 目录 |
| Scheduler | `config` 中每个 job 声明 `assistant_id`，触发时以该助手身份跑完整 Loop；若该助手正忙则跳过本次并记日志 |
| API | `GET/POST /api/assistants`、`DELETE /api/assistants/{id}`（默认归档）；`POST /api/tasks` 必带 `assistant_id`；`/api/articles` 的 `assistant_id` **必填** |
| CLI | `python -m agent --assistant tech-writer "写一篇..."`，缺省用 `default`；`--resume <session_id>` 续接会话 |

### 4.6 并发控制（同一助手任务串行）

Scheduler、CLI、API 三个入口都可能让同一个助手同时跑两个 Loop，会引发 profile.md 写竞争与文章文件冲突。**注意 CLI 与 FastAPI 是独立进程、多开 CLI 窗口也是多进程，进程内内存锁互不可见，因此锁必须跨进程。** 策略：

- **锁落数据库**：app.db 内置 `run_locks` 表（`assistant_id` 主键 + `task_id` + `pid` + `acquired_at`）。获锁 = `INSERT` 成功（主键冲突即被占）；释放 = 任务结束 `DELETE` 该行。SQLite 单文件 + WAL 下 INSERT 主键互斥**天然跨进程**，CLI、FastAPI、多个 CLI 窗口看到的是同一把锁。
- **崩溃残留回收（TTL + PID 存活校验双保险）**：获锁前先处理过期行——`acquired_at` 距今超过 `RUN_LOCK_TTL`（默认 2 小时，覆盖长文任务合理时长）的锁行进入回收流程，但**回收前先用 `pid` 列做存活校验**（Windows 上 `OpenProcess` / POSIX 上 `os.kill(pid, 0)`）：进程已死 → 确认是强杀残留，删行后重试插入；进程仍存活 → 说明任务还在跑只是超时，**不回收**，按"被占用"走拒绝路径并提示该任务已超时应人工检查。进程正常退出总会删行，走到回收流程的只剩崩溃/强杀场景。
- **后来者拒绝，不排队**（个人工具场景下"拒绝"比静默排队更可预期）：API 返回 `409 Conflict` + 当前运行中的 `task_id`；CLI 直接报错退出并提示；Scheduler 跳过本次触发并记 warning。
- 进程内可保留一层 `asyncio.Lock` 作为快路径（避免同进程并发协程频繁打 DB 主键冲突），但**正确性由 `run_locks` 表保证**，内存锁只是优化。

### 4.7 文章项目、受管文件与 AI 修改

阶段 4 的 Web 主对象是**文章项目**，不是聊天会话。一个助手可以拥有多个项目；一个项目对应 `data/assistants/<assistant_id>/projects/<project_id>/` 下的一个受管文件夹，可包含正文、章节、素材和图片。项目内 `.md`、`.markdown`、`.txt` 是可编辑文档，其他普通文件可显示在资源树中但本阶段不承诺文本编辑。

导入一律采用“复制到受管目录”语义，不保留外部路径引用或双向同步：

- 导入文件夹：创建一个项目并递归复制目录树，保留合法相对路径；一个导入文件夹就是一个文章项目。
- 导入单个 `.md`、`.markdown` 或 `.txt`：按源文件名创建项目文件夹，再把原文件复制进去并作为首次打开文档。
- 同名项目使用稳定 `project_id` 与显示名分离，不覆盖已有目录；拒绝绝对路径、`..`、符号链接/重解析点和越过项目根目录的路径。
- Web 通过文件/文件夹选择器上传内容，API 根据相对路径重建目录树；浏览器不向服务端授权任意本地路径读取。
- 单次导入默认限制为 5000 个文件、总计 512 MiB、单文件 100 MiB，配置可收紧；校验在正式项目可见前完成，超限则整次拒绝。

项目文件是正文的事实来源，SQLite 保存项目、文件身份、版本、任务元数据与短生命周期写入意图。每个可编辑文件拥有稳定 `document_id`、项目内相对路径和单调递增的 `document_version`；不在本阶段实现完整版本历史。所有文件读取、保存、导入和树查询必须同时校验 `assistant_id` 与 `project_id`，并通过 MemoryStore 完成元数据操作。

文档保存与 change set 应用必须先在 `BEGIN IMMEDIATE` 中完成归属、版本、状态和原文快照校验，再登记带目标内容、BOM 策略、PID、进程启动时间和认领时间的持久化写入意图。写入意图的恢复分成“短事务认领 → 事务外创建临时文件并原子替换 → 短事务终结”三步；终结时必须再次核对意图身份，避免持有 SQLite 写锁执行磁盘 IO。恢复阶段的文件系统写入失败时必须保留意图并释放本次认领，使后续操作可以重试；新保存/apply 在首次文件写入失败时则撤销尚未生效的意图。冲突请求在校验失败时不得写临时文件或恢复正文。进程在文件替换与元数据终结之间退出时，下一次读取/写入由 MemoryStore 根据意图与内容摘要完成恢复或终结，不能留下“磁盘已改、版本未增”的永久分叉。新写入意图用 PID + 进程启动时间判断存活；缺少启动时间的旧意图最多阻塞到 TTL，PID 复用不得造成永久阻塞。

项目支持新建空白项目（默认创建 `article.md`）、重命名显示名和归档删除。归档将整个项目目录移动到 `data/archive/projects/<assistant_id>/<project_id>-<UTC 时间戳>/`，不复用项目 id；运行中或存在未应用 change set 时拒绝归档。物理清除只允许显式 `purge=true`。MemoryStore 启动时必须对账项目级崩溃残骸：未注册的 `.import-*` staging 删除；`.purge-*` 在项目元数据仍存在时恢复到登记位置、元数据已删除时清除；活动项目目录缺失但存在唯一同 id 归档半程目录时移回活动目录。对账只处理受管目录和可由数据库唯一归属的路径。

用户在编辑器中选中文本后可以提交局部改写指令。请求携带 `assistant_id`、`project_id`、`document_id`、选区文本、选区范围、指令和生成时看到的 `document_version`。Runtime 校验归属并按助手获取既有运行锁，再使用 persona 与 editing Skill 生成 change set。change set 只保存建议，不直接写正文；用户接受时再次校验版本和原文快照，通过后原子写文件、递增版本并标记 applied，冲突则返回 409。

选区范围采用半开区间 `[from, to)`，单位为 Unicode code point，而不是 JavaScript UTF-16 code unit 或 UTF-8 byte；前端在发送前转换，后端以同一单位切片并用 `selected_text` 二次校验，确保中文、emoji 和组合字符不会错位。

可编辑导入文件只接受 UTF-8 或 UTF-8 BOM。API 返回给编辑器的正文不含 BOM，但文件首次导入时是否携带 UTF-8 BOM 必须在后续手工保存、change set 应用和崩溃恢复中保持，不能静默改变文件编码标记。

右侧 Agent 面板绑定当前助手与项目。聊天可读取当前文件、明确附加的其他项目文件和本助手记忆；涉及内容修改时也必须返回 change set/diff，经用户确认后才写入。一次模型响应包含多个修改时，MemoryStore 必须先验证全部目标文档、版本、范围和原文快照，再在单个事务中批量创建 change set；任何一项非法则整批失败，不留下不可见的 pending 建议。聊天不能在未明确项目时修改文件，也不能访问其他助手项目。选区改写、聊天修改和普通写作任务共享 AgentRuntime、EventBus、AssistantRegistry、MemoryStore、MCP/Skill 注册表及运行锁，不另建 LLM 调用链路。

---

## 5. 模块职责（输入 / 输出 / 交互）

### 5.1 `agent/planner.py` — 规划器

| 项 | 内容 |
|----|------|
| 输入 | `AgentState`（任务、裁剪后的观察、本助手可用的 Skill 元数据、工具清单、`memory_context`、人设） |
| 输出 | `PlanModel`（见下），写入 `state.plan` |
| 交互 | 调用 LLM（`response_format` 强约束 JSON）；失败处理见下方"降级路径" |

```python
class ActionPlan(BaseModel):
    thought: str                          # 当前局势判断（展示给用户）
    next_action: Literal["call_tool", "activate_skill", "write", "finish"]
    skill: str | None                     # 要激活的 Skill 名
    skill_reason: str | None              # ★ 选择理由（验收关键证据）
    tool_calls: list[ToolCall]            # 本轮工具调用（可并行）
    tool_reason: str | None               # ★ 工具选择理由
    done_criteria_met: bool               # 是否判定任务完成
```

**Prompt 中注入的内容**：助手人设（persona）、任务、裁剪后的观察（§3.3）、Skill 清单（仅 `name` + `when_to_use`）、工具清单（`name` + `description` + 参数 schema）、`memory_context`。**理由字段必填**，SSE 直接透传给前端展示。

**降级路径（必须可路由）**：Pydantic 校验失败 → 把错误信息回喂重试 1 次 → 仍失败则**不再产出自由文本**，而是直接构造 `PlanModel(next_action="finish", thought="规划器连续输出非法 JSON，终止并保留已有成果")`：若已有 `draft` 则照常落盘并在文末标注"异常终止"；若无 draft 则 `status=failed`、落日志、事件总线发 `failed` 事件。任何路径下状态机都有合法出边。

### 5.2 `agent/executor.py` — 执行器

| 项 | 内容 |
|----|------|
| 输入 | `PlanModel.tool_calls` + `UnifiedToolRegistry` |
| 输出 | `list[Observation]`（结构化摘要，非原始全文，见 §3.3） |
| 交互 | 并行执行独立工具调用（`asyncio.gather`），单工具超时 30s，失败重试 1 次后把错误作为观察返回（让 Planner 自己决定是否换工具） |

**工具上下文注入（ToolContext）**：统一工具协议的调用签名为 `call(args: dict, ctx: ToolContext)`。`ToolContext` 携带当前任务的 `assistant_id` / `session_id` / `data_dir`，由 Executor 在每次调用时注入——工具注册表是进程启动时一次性构建的，而任务是按助手运行的，内置工具（如 `finalize_article` 要写 `data/articles/<assistant_id>/` 并登记索引）正是通过 `ctx` 获得归属信息。`ctx` **不出现在暴露给 LLM 的 JSON Schema 中**，Planner 无法伪造；MCP 工具不需要 ctx，适配层直接丢弃。

**错误即观察**原则：工具抛异常不打断 Loop，而是变成 `Observation(success=False, error=...)` 交回 Planner——这是 Agent 自愈能力的基础。

### 5.3 `agent/loop.py` — 状态机装配

observe / plan / act / reflect / **write 五个节点全部在本模块装配**（write 节点的大纲→逐节成文→合并编排逻辑量较大，是独立节点函数，不散落在别处）。挂载：

- **事件回调**：每个节点进出、每次工具调用、每个 LLM token 都发事件到 `EventBus`（阶段 2 打印到终端，阶段 4 桥接 SSE）。
- **Checkpoint**：`langgraph-checkpoint-sqlite`，`thread_id = <assistant_id>:<session_id>`，会话可恢复且天然按助手隔离。

### 5.4 `agent/runtime.py` — 运行时入口

组装顺序（进程启动时执行一次）：

1. 加载 `.env` 与 `config/settings.py`
2. 初始化 LLM client（OpenAI 兼容）
3. 启动 MCP Client，连接 `mcp_servers.json` 中的全部 server，`list_tools()` 发现工具
4. 扫描 `skills/` 目录，注册 Skill 元数据
5. 构建 Unified Tool Registry = 内置工具（`agent/tools.py`：save_markdown / read_file / finalize_article，**全部沙箱限制在 `data/` 内**）+ MCP 工具
6. AssistantRegistry 扫描 `data/assistants/`，加载全部助手定义（缺 `default` 则自动创建）
7. 初始化 Memory（SQLite 建表，**开启 WAL**，建 FTS5 trigram 虚拟表与 `run_locks` 表）
8. 编译 LangGraph 状态机
9. 仅当以 `schedule` 长驻模式启动时，在**当前 Runtime 的 asyncio 事件循环**上注册并启动 APScheduler；一次性 `run` 不启动 Scheduler

每次任务：`runtime.run(assistant_id, task)` → **按助手获取运行锁（占用则拒绝，§4.6）** → 取该助手 persona + 技能子集 → `recall` 一次写入 `state.memory_context` → 进入 Loop。

CLI 入口为 `agent/__main__.py`（`python -m agent` 的载体），子命令：`run`（默认）、`assistants list/create/delete`、`--resume`。`run` 命令从 Runtime 启动开始即进入 `try/finally` 清理边界：启动或任务执行发生未预期异常时发出 failed 事件并返回非零退出码，已分配的 Store/MCP 等资源仍由 `runtime.close()` 释放。

### 5.5 Skill 系统 — `skills/`

**格式严格对齐 Claude Code 的渐进式披露模型**：启动时只解析 frontmatter，正文按需加载。

```
skills/
  research/
    SKILL.md      # frontmatter: name / description / when_to_use；正文: 搜索→筛选→归纳工作流
    tools.yaml    # 声明依赖: [tavily_search, fetch]
  writing/SKILL.md   # 大纲→逐节成文→合并；依赖 [save_markdown]
  editing/SKILL.md   # 润色/事实核查/查重；依赖 [fetch, save_markdown]
```

`SKILL.md` frontmatter 示例：

```yaml
---
name: research
description: 网络资料搜索、来源筛选与关键观点归纳
when_to_use: 任务涉及外部事实、时效性信息、需要引用来源时激活
---
```

**激活机制与依赖校验**：Planner 决定 `activate_skill` 后，Skill Manager **先校验 `tools.yaml` 声明的依赖是否都在当前工具表中**：
- 全部可用 → 注入正文 prompt + 标记推荐工具，激活成功；
- 有缺失（如 Tavily 未配置）→ **激活失败**，返回 `Observation(success=False, error="skill 'research' 缺少依赖工具: tavily_search")` 交回 Planner，由其决定：换用其他可用工具徒手检索、跳过该 Skill、或 finish 并向用户说明。Planner 决策理由照常落日志，可观测。

同一任务可激活多个 Skill（research + writing 串联是常态）。

**与助手的关系**：`skills/` 目录全局共享（能力是公共的），但每个助手通过 `assistant.yaml` 的 `skills` 列表裁剪自己可用的子集——Planner 只能看到本子集。

### 5.6 MCP 层 — `mcp_client/`

- `registry.py`：读取 `config/mcp_servers.json`，字段与 Claude Desktop 配置兼容（`command` / `args` / `env`）。**注意**：`${VAR}` 环境变量插值和内置占位符 `${PROJECT_ROOT}` 是**本实现的超集扩展**，Claude Desktop 原生格式不支持——从 Claude Desktop 迁移配置过来没问题，反向迁移需手动展开变量。
- `client.py`：基于 `mcp.client.stdio.stdio_client`，每个 server 一个长连接子进程；启动时 `list_tools()`，把每个 MCP tool 包装成 `ToolSpec` 注册进统一工具表；`call_tool` 时路由到对应 server。

`mcp_servers.json` 示例（**默认不注册 filesystem**，理由见下）：

```json
{
  "mcpServers": {
    "tavily": {
      "command": "npx",
      "args": ["-y", "tavily-mcp"],
      "env": { "TAVILY_API_KEY": "${TAVILY_API_KEY}" }
    },
    "fetch": {
      "command": "uvx",
      "args": ["mcp-server-fetch"]
    }
  }
}
```

> **filesystem MCP 为何移出默认配置**：内置工具已覆盖 `data/` 目录的全部读写需求，且 `finalize_article` 会同步登记 articles 索引——若 Planner 改用 filesystem 的 `write_file` 直接写文章，会**绕过索引表**，导致文章管理功能看不到该文章。需要让 Agent 访问 `data/` 之外的目录时，可自行添加 filesystem server（作用域建议仍限制在 `${PROJECT_ROOT}/data`）。
>
> 外部前置依赖：Node（npx，跑 tavily）与 uv（uvx，跑官方 Python fetch server）；`requirements.txt` 含 `uv`，README 写明 Node 需自装。

### 5.7 Memory 层 — `memory/`

| 层 | 载体 | 内容 | 读写时机 |
|----|------|------|---------|
| 短期 | SQLite `sessions` / `messages` 表（含 `assistant_id`）+ LangGraph checkpoint | 当前任务上下文、对话历史、工具观察 | 每步写；任务开始时读 |
| 素材 | SQLite `sources` 表（含 `assistant_id` + `session_id`；字段：url / title / fulltext / fetched_at） | fetch 抓取的网页全文，供 Reflect 核查与事后备查 | Executor 抓取后写入；不进对话上下文（§3.3）；查询同样强制按 `assistant_id` 过滤 |
| 长期 | `assistants/<id>/memory/profile.md`（写作风格/偏好/常用主题）+ SQLite `articles` 表（含 `assistant_id`） | 跨任务、跨会话沉淀 | **任务启动时 `recall` 一次，结果存 `state.memory_context`，Planner 每轮从 state 注入**；Reflect 判定有新偏好时 `memorize` |
| 项目工作区 | `data/assistants/<id>/projects/<project_id>/` + SQLite `projects` / `project_documents`（均含 `assistant_id`） | 受管文件树、可编辑文档身份和 `document_version` | 导入/树查询/文件保存均经 API 与 MemoryStore；内容以受管文件为事实来源 |
| AI 修改建议 | SQLite `change_sets`（含 `assistant_id`、`project_id`、`document_id`、`session_id`、原文范围/快照、替换文本、基准版本、来源模式、状态） | 选区改写或聊天产生的待确认修改 | 生成时写入 pending；应用/拒绝/过期均按助手、项目与文档归属校验 |

- `store.py` 统一接口（`assistant_id` 恒为第一参数）：

```python
def recall(assistant_id: str, query: str, *, limit: int = 10) -> str
def memorize(
    assistant_id: str,
    kind: Literal["preference", "style", "topic", "article"],
    content: str,
    *,
    session_id: str | None = None,
) -> None
def recall_semantic(assistant_id: str, query: str) -> str  # 预留向量接口，阶段 2-3 抛 NotImplementedError
```

`kind="article"` 写 articles 索引表；其余 kind 增量改写本助手 `profile.md`。

- **中文检索定案：SQLite FTS5 `trigram` 分词器**（SQLite ≥3.34 内置，Python 3.12 自带版本满足）。`messages_fts` / `articles_fts` 作为原表的**外部内容索引**，由 INSERT/UPDATE/DELETE 触发器保持同步。FTS schema 用 SQLite `PRAGMA user_version` 标记完整迁移版本：虚拟表缺失、tokenizer 非 trigram、触发器不齐或版本未完成时，均在单个事务内删除旧索引、重建并 `rebuild` 历史行，只在回填成功后写入版本号。trigram 对中文子串匹配有效且无需外分词依赖；`recall` 走 `MATCH`，再 join 原表并显式以 `assistant_id` 过滤，结果按 BM25 相关度排序。中文长任务描述会抽取三字窗口并在**全部候选词元范围内均匀采样**为最多 16 项的有界 OR 查询，兼顾查询长度与尾部主题词。放弃默认 unicode61（对中文无效）；jieba 等外分词作为后期可替换项（检索接口不变）。**短词元回退**：`query.strip()` 不足 3 个字符，或按空白拆分后没有任何长度至少 3 的可用词元时，对各非空词元执行参数化 OR LIKE；查询中的 `\\`、`%`、`_` 均按字面量转义。LIKE 模式同样最多 16 项，超限时在全部去重词元中均匀采样并保留首尾（空查询不检索），保证 recall 不静默漏检、不扩大匹配范围，也不生成无上限 OR 子句。
- **检索故障降级**：profile、文章 FTS、消息 FTS、文章 LIKE、消息 LIKE、最近文章六路读取分别隔离异常并记 warning；任一路损坏时继续组合其余可用结果。FTS 查询失败时最近文章仍可兜底，任何 recall 存储故障都不得阻断 Agent 写作主链路。
- **同助手跨会话共享**：`recall` 的检索范围 = 本助手全部历史会话 + 本助手 profile.md；**跨助手隔离**：SQL 强制 `WHERE assistant_id = ?`，profile 按目录物理隔离。
- **项目与建议隔离**：`projects`、`project_documents`、`change_sets`、`document_write_intents` 的所有查询必须同时校验 `assistant_id`；`project_id` / `document_id` 不能单独作为授权或查询条件。保存文档和应用 change set 必须在写事务内执行版本号、状态与原文快照校验，使用持久化写入意图 + 临时文件 + 原子替换更新正文；冲突方不得触碰磁盘，进程崩溃后的残留意图必须可恢复。聊天产生多条建议时使用 MemoryStore 批量接口原子创建。
- **工具写边界**：`save_markdown` 只允许写 `data/` 下的非受管中间产物，必须拒绝 `assistants/<assistant_id>/projects/` 及任意其他助手项目路径；项目正文只能经项目文档/change set API 修改。

### 5.8 Scheduler — `scheduler/`

- `scheduler.py`：APScheduler `AsyncIOScheduler`，cron 表达式注册在 `config/settings.py` 的 `JOBS` 列表里，**每个 job 声明 `assistant_id`**：

```python
JOBS = [
    {
        "id": "daily-ai-news",
        "assistant_id": "default",
        "cron": "0 8 * * *",
        "task": "搜索今日 AI 新闻，生成技术日报并保存为 Markdown",
    },
]
```

- `jobs.py`：任务函数 = 以指定助手身份调用 `runtime.run(assistant_id, task)` 跑一次完整 Agent Loop；助手正忙（锁被占）则跳过本次并记 warning。
- **生命周期**：Scheduler 由 Runtime 持有，但只在 `python -m agent schedule` 长驻命令中启动；创建 `AsyncIOScheduler` 时显式绑定 `asyncio.get_running_loop()`，使 cron 回调与 `runtime.run` 在同一事件循环内执行。Runtime 关闭时先停 Scheduler，并等待已取消 job 完成 `finally` 清理（尤其是释放 `run_locks`），再关 MCP 与 Memory。
- **注册容错**：内置 job 默认绑定 AssistantRegistry 会自动创建的 `default` 助手。每个 job 校验 `id` / `assistant_id` / `cron` / `task`；助手不存在或 cron 非法时只跳过该 job 并记 warning，不阻断其他 job。注册时使用 `coalesce=True`、`max_instances=1` 和 `misfire_grace_time=60`：防止进程暂停后积压补跑，休眠或停机错过超过 60 秒的触发不补跑；不同 job 抢同一助手时仍以 `run_locks` 为最终互斥边界。
- README 附带 Windows Task Scheduler XML 导出说明（`schtasks /create /xml`），实现开机自启交给系统，不自造守护进程。

### 5.9 API 层 — `api/`（阶段 4，接口现在定）

| 端点 | 说明 |
|------|------|
| `GET /api/assistants` | 助手列表；`POST /api/assistants` 创建新助手（写 `assistants/<id>/` 目录）；`DELETE /api/assistants/{id}` 归档删除（`?purge=true` 级联清理） |
| `POST /api/tasks` | 提交任务（body 必含 `assistant_id`），入队前同步校验助手存在且未被运行锁占用；未知助手返回 404，正忙返回 409，成功返回 `task_id` |
| `GET /api/tasks/{id}?assistant_id=X` | 查询任务终态；任务记录绑定创建时的 `assistant_id`，跨助手查询按 404 处理 |
| `GET /api/tasks/{id}/stream?assistant_id=X` | SSE：按订阅者独立队列广播 `thought` / `tool_call` / `token` / `done` / `failed` 事件；跨助手按 404 处理 |
| `GET /api/projects?assistant_id=X` | 当前助手的文章项目列表；`assistant_id` 必填 |
| `POST /api/projects` | 新建空白文章项目并创建 `article.md`；body 必含 `assistant_id` 与显示名 |
| `PATCH /api/projects/{project_id}` | 重命名项目显示名；不改变稳定 `project_id` 或物理目录 |
| `DELETE /api/projects/{project_id}` | 默认归档项目；`purge=true` 才物理清除，均须校验 `assistant_id` 和运行/待应用状态 |
| `POST /api/projects/import-file` | multipart 导入单个 `.md`/`.markdown`/`.txt`；必含 `assistant_id`，自动创建项目文件夹并复制文件 |
| `POST /api/projects/import-folder` | multipart 导入文件夹；必含 `assistant_id` 和每个文件的合法相对路径，在助手受管目录中重建项目树 |
| `GET /api/projects/{project_id}/tree?assistant_id=X` | 返回项目资源树；必须校验项目属于该助手 |
| `GET /api/projects/{project_id}/documents/{document_id}?assistant_id=X` | 读取可编辑文件及当前 `document_version` |
| `PUT /api/projects/{project_id}/documents/{document_id}` | 保存手工编辑；body 必含 `assistant_id`、正文和期望版本，冲突返回 409 |
| `POST /api/projects/{project_id}/documents/{document_id}/selection-rewrites` | 创建选区局部改写任务；body 必含 `assistant_id`、选区文本/范围、指令和版本；返回 `task_id`/`change_set_id` |
| `POST /api/projects/{project_id}/change-sets/{change_set_id}/apply` | 显式应用选区或聊天 change set；body 必含 `assistant_id` 和期望版本；快照或版本不匹配返回 409 |
| `POST /api/projects/{project_id}/change-sets/{change_set_id}/reject` | 拒绝 change set；必须校验助手、项目和文档归属 |
| `POST /api/projects/{project_id}/agent/messages` | 向项目 Agent 面板发送消息；body 必含 `assistant_id`，可带当前 `document_id`、选区及显式附件；修改类结果仍以 change set 返回 |
| `GET /api/articles?assistant_id=X` | 既有完成态文章归档列表；`assistant_id` 必填。它不是项目编辑入口 |
| `GET /api/articles/{id}?assistant_id=X` | 只读获取完成态文章；要继续编辑须复制/导入为项目，所有保存统一走项目文档 API |
| `GET /` | 托管 `web/dist` 静态文件 |

`TaskBroker` 是 EventBus 与 SSE 的桥接层，Runtime 对 SSE 零感知——阶段 2 的 CLI 和阶段 4 的 Web 复用同一个 Runtime。每条任务记录包含 `assistant_id`，每个 SSE 连接使用独立通知队列，取消也必须进入终态。终态记录按 TTL/容量有界保留，事件历史只为断线重放服务，不得无限增长。所有返回 202 的任务创建端点必须在入队前校验助手存在且当前没有有效运行锁；未知助手返回 404，已忙返回 409，不得先创建注定失败的任务或在异步错误中泄漏助手列表。`api.main` 只提供 `create_app` 工厂；生产入口为 `api.server:app`，避免导入 API 模块时打开真实数据库。局部改写与聊天沿用任务流，并新增 `change_preview` 事件（含 `change_set_id`、项目/文档 id、原文范围、建议文本和基准版本）；正文只有在 apply 成功后更新。

API 层不通过错误文本猜测冲突类型。MemoryStore/项目存储以专用冲突异常表达版本冲突、待处理状态和跨进程写入占用，API 稳定映射为 HTTP 409；参数错误保持 400，资源不存在保持 404。

### 5.10 前端 — `web/`（阶段 4）

Vue 3 + Vite 单页采用 VS Code 式写作 IDE，而非聊天主界面：顶部/活动栏提供**助手选择器**和项目导入；左侧资源管理器列出当前助手的项目及项目文件树；中间为多标签 CodeMirror 编辑器，可切换 Markdown 预览；右侧为绑定当前项目的 Agent 面板，显示对话、执行事件、来源和待确认修改。切换助手时必须关闭或重新加载不属于新助手的项目标签和 Agent 会话。

选中文本后显示锚定工具栏，含提示词输入和生成按钮；生成期间保留 CodeMirror 选区状态，返回后以 diff 显示原文与建议文本，并提供接受、拒绝、重新生成。Agent 面板的聊天可作用于当前文件、当前选区或显式附加文件；若产生文件修改，同样进入 change set 预览，不直接覆盖。Markdown 预览把文档和模型输出视为不可信输入，`marked` 解析结果必须经过 HTML 消毒后才能交给 `v-html`。

每个编辑标签必须保存自己的 `project_id`；保存使用标签页归属，应用/拒绝使用 change set 归属，不能依赖资源树当前选中的项目。外部接受修改后必须把服务端正文同步进 CodeMirror 与 store；若目标标签 dirty，必须先显式确认丢弃未保存内容。Agent 面板的待审卡片只能在父级确认 apply/reject 成功后移除，请求失败必须保留以便重试。关闭 dirty 标签或离开页面同样需要保护。组件切换助手、项目、文档或卸载时必须关闭所属 EventSource，网络/解析错误必须退出 loading 状态；异步 POST 返回和旧任务事件都需校验发起时的助手、项目和文档作用域，不能注入新上下文。

前端只能通过 API 读写，不能直接读取 SQLite、助手目录或外部原路径。`npm run build` 产物由 FastAPI 托管，单进程交付。

---

## 6. 关键数据流

### 6.1 一次写作任务的完整时序

```mermaid
sequenceDiagram
    participant U as 用户
    participant R as Runtime
    participant P as Planner(LLM)
    participant E as Executor
    participant M as MCP Servers
    participant MEM as Memory(按助手隔离)

    U->>R: run("tech-writer", "写一篇关于模型蒸馏的文章")
    R->>MEM: 写入 run_locks 获锁（主键冲突则拒绝）
    R->>R: 加载 tech-writer 人设 + 技能子集
    R->>MEM: recall("tech-writer", 任务) —— 仅启动时一次
    MEM-->>R: 本助手风格画像 + 文章索引 → state.memory_context
    loop Agent Loop (max 25 步)
        R->>P: 人设 + 任务 + 裁剪后观察 + memory_context + Skill/Tool 清单
        P-->>R: PlanModel(激活 research · 理由 · 调 tavily)
        R->>E: 执行 tool_calls
        E->>M: tavily_search / fetch
        M-->>E: 搜索结果 / 网页正文
        E-->>R: Observations（摘要化）
    end
    R->>P: 素材齐备 → 激活 writing skill
    P-->>R: 大纲 → 逐节成文（token 流）
    R->>R: Reflect 按 checklist 质检
    R->>MEM: memorize("tech-writer", kind=article/preference, ...)
    R-->>U: data/articles/tech-writer/模型蒸馏-20260806-0934.md
```

### 6.2 SSE 事件模型（阶段 4 用，阶段 2 先落日志）

```json
{"type": "thought",   "data": {"text": "需要先搜集资料，激活 research skill", "step": 2}}
{"type": "tool_call", "data": {"tool": "tavily_search", "args": {}, "reason": "..."}}
{"type": "tool_result","data": {"tool": "tavily_search", "ok": true, "summary": "..."}}
{"type": "token",     "data": {"text": "…正文流式片段…"}}
{"type": "change_preview", "data": {"change_set_id": "...", "project_id": "...", "document_id": "...", "range": {"from": 10, "to": 24}, "replacement": "…建议替换文本…", "document_version": 7}}
{"type": "done",      "data": {"path": "data/articles/tech-writer/模型蒸馏-20260806-0934.md"}}
{"type": "failed",    "data": {"reason": "..."}}
```

---

## 7. 技术选型理由与替代方案

| 选择 | 理由 | 放弃的方案 & 原因 |
|------|------|-------------------|
| LangGraph | 状态机天然匹配 Agent Loop；`astream_events` 原生支持 token 级流式；SQLite checkpoint 解决会话恢复（`thread_id` 含 assistant_id，天然隔离） | AutoGen（多Agent对话框架，单人写作场景过重）；手写 while 循环（流式/恢复/可观测性都要自己造） |
| OpenAI 兼容 client | DeepSeek/通义/Kimi/OpenAI 一个接口全覆盖，国内网络可用 | 各家原生 SDK（Vendor lock-in，无收益） |
| 官方 `mcp` SDK | 需求明确禁止自造协议；stdio 是本地工具最稳的传输 | HTTP/SSE 传输（本地单用户无必要，多一层网络栈） |
| SQLite（WAL 模式） | 零部署、事务；**FTS5 trigram 解决中文检索**（见 §5.7）；`assistant_id` 列即可实现逻辑隔离 | PostgreSQL（单用户杀鸡用牛刀）；每助手一个 DB 文件（无法跨助手管理，且会话表碎片化）；LIKE 检索（全表扫描且中文体验差） |
| 助手定义为文件（YAML + Markdown） | 白盒、可手改、可 git 管理；与 profile.md 同目录，迁移一个助手 = 拷贝一个文件夹；删除 = 移动归档 | 存数据库（增删助手要走 UI/SQL，违背个人工具原则） |
| APScheduler | 进程内 cron，和 FastAPI 同一事件循环 | Celery（需要 broker，过度设计） |
| Vue 3 + Vite | 生态熟悉度最高、构建产物纯静态可托管 | React（同等可行，纯偏好）；不选 Next.js（无 SSR 需求） |
| Pydantic v2 | PlanModel/ToolSpec 强类型 + JSON Schema 导出（直接喂给 LLM 的 structured output） | dataclass + 手写校验 |

---

## 8. 目录结构（最终态，阶段 2 先建带 ★ 的部分）

```
writing-agent/
  agent/           ★ __main__.py / runtime.py / planner.py / executor.py / loop.py
                     ★ events.py / schemas.py / tools.py（内置沙箱工具）/ assistant_registry.py
  skills/          ★ research/ writing/ editing/（各含 SKILL.md + tools.yaml）
  mcp_client/      ★ client.py / registry.py（支持 ${VAR} 与 ${PROJECT_ROOT} 插值）
  memory/          ★ short_term.py / long_term.py / store.py（assistant_id 贯穿，阶段 3 充实）
  scheduler/         scheduler.py / jobs.py                       （阶段 3）
  api/               main.py / routes.py / sse.py                 （阶段 4）
  web/               Vue 3 + Vite                                 （阶段 4）
  config/          ★ settings.py / mcp_servers.json
  data/            ★ app.db（WAL）
                     ★ assistants/<id>/assistant.yaml + persona.md + memory/profile.md
                     assistants/<id>/projects/<project_id>/（阶段 4 受管文章项目）
                     ★ articles/<assistant_id>/*.md
                     项目/文档身份、当前 `document_version` 与 change set 由 app.db 管理
                     archive/<id>-<ts>/（删除助手的归档，运行时自动生成）
  docs/            ★ phase1-architecture.md / phase1-architecture-review.md
  tests/           ★ test_skill_loading.py / ★ test_tool_registry.py / ★ test_memory_isolation.py
                     （三个测试全部随 MVP 交付）
  .env.example     ★
  requirements.txt ★
  README.md        ★
```

`.env.example`：

```bash
OPENAI_API_KEY=sk-xxx
OPENAI_BASE_URL=https://api.deepseek.com
MODEL_NAME=deepseek-chat
TAVILY_API_KEY=tvly-xxx
MAX_STEPS=25
# 运行锁过期时间（小时），超过后结合 PID 存活校验回收崩溃残留锁，见 §4.6
RUN_LOCK_TTL=2
# 阶段 4 项目导入上限
PROJECT_IMPORT_MAX_FILES=5000
PROJECT_IMPORT_MAX_TOTAL_MB=512
PROJECT_IMPORT_MAX_FILE_MB=100
```

> 注 1：内置工具与节点归属——observe/plan/act/reflect/write 节点在 `agent/loop.py` 装配；内置工具（save_markdown / read_file / finalize_article）在 `agent/tools.py`；助手注册在 `agent/assistant_registry.py`（代码模块不放顶层 `assistants/` 包，避免与数据目录 `data/assistants/` 同名混淆）。
>
> 注 2：`finalize_article` 与 `save_markdown` 的分工——`save_markdown(path, content)` 是纯写文件（沙箱限 `data/`），用于大纲、素材笔记等中间产物；`finalize_article(title, content)` 是**完成态文章**的收口：写入 `data/articles/<assistant_id>/<标题>-<时间戳>.md`（时间戳后缀防同名覆盖）+ 登记 articles 索引 + 触发 `memorize(kind="article")`。
>
> 注 3：`test_memory_isolation.py` 专测隔离红线——助手 B 的 recall/memorize 结果中不得出现助手 A 的任何数据，这是多助手架构的核心回归测试，随 MVP 交付。

---

## 9. 错误处理与边界

| 场景 | 策略 |
|------|------|
| LLM 返回非法 JSON（Planner） | Pydantic 校验失败 → 错误回喂重试 1 次 → 仍失败则**强制构造可路由的 finish**（有 draft 照常落盘并标注异常终止；无 draft 则 `status=failed`），见 §5.1 |
| MCP server 启动失败 | 记 warning，该 server 工具不注册，**不阻断启动**（Planner 会看到工具表里没有它） |
| profile 或 SQLite recall 读取失败 | 分路记 warning 并继续组合其余 profile / FTS / LIKE / 最近文章结果；检索故障不阻断写作任务，见 §5.7 |
| CLI Runtime 启动或任务执行出现未预期异常 | 发 failed 事件并返回非零退出码；`finally` 始终关闭 Runtime 已分配资源，见 §5.4 |
| **Skill 依赖工具缺失**（如 Tavily 未配置时激活 research） | 激活失败，返回结构化 Observation 交回 Planner 决策（换工具 / 跳过 / finish 并说明），见 §5.5 |
| 工具执行异常 | 包装成 `Observation(success=False)` 交还 Planner 决策 |
| 超过 max_steps | 强制 finish：基于已有素材成文或输出部分结果 + 原因 |
| 单工具超时 | 30s asyncio timeout，重试 1 次 |
| 长文截断 | writing skill 强制分段：先大纲，每节独立 LLM 调用，最后合并 |
| Reflect 连续 3 次质检未过 | 强制 finish，文末标注存疑项（§3.4） |
| 指定的 assistant_id 不存在 | Runtime 拒绝启动任务并列出可用助手（CLI/API 行为一致） |
| **同一助手并发任务** | `run_locks` 插入冲突 → 拒绝：API 409 / CLI 报错 / Scheduler 跳过（§4.6） |
| 删除运行中的助手 | 先查 `run_locks`，该助手有任务在跑则拒绝删除（Windows 下移动被占用目录会失败），见 §4.2 |
| 助手目录损坏（YAML 解析失败） | 该助手标记不可用并 warning，不影响其他助手与系统启动 |
| 文章同名 | `finalize_article` 自动加时间戳后缀，不覆盖（§8 注 2） |
| 项目或文档不属于请求助手 | API 与 MemoryStore 均返回 404，不暴露其他助手资源是否存在 |
| AI change set 返回后正文已被编辑 | apply 校验 `document_version` 与原文快照；任一不匹配返回 409，前端重新读取正文和选区后再生成 |
| 文档保存/apply 在并发校验时发现版本冲突 | 写事务失败且不得创建/替换正文文件；返回 409，不执行“恢复旧内容”覆盖其他进程已提交结果 |
| 文件原子替换后进程退出或 SQLite 元数据未终结 | 保留 `document_write_intents`；下次 MemoryStore 读取/写入按目标内容摘要恢复并完成版本/change set 状态终结，不允许永久分叉 |
| 选区改写任务与同助手长任务并发 | 复用 `run_locks`；后来者遵循既有 API 409 / CLI 报错 / Scheduler 跳过语义，不排队 |
| AI 改写输出非法或为空 | 标记该局部任务 failed，保留原选区与正文，不创建可应用建议 |
| 导入目录包含 `..`、绝对路径、符号链接或重解析点 | 拒绝对应导入并清理本次未完成的临时目录；不得在项目根外创建文件 |
| 导入中途失败或项目同名 | 先写受管目录内的临时项目，全部完成后原子改名；显示名可相同，`project_id` 与物理目录不得覆盖 |

---

## 10. 阶段 2/3/4 的接口预留

- **阶段 2（MVP）**：CLI 入口 `python -m agent run --assistant tech-writer "写一篇关于 X 的文章"`；`assistant_id` 从第一天就贯穿 store/checkpoint/输出目录/运行锁；EventBus 打印彩色终端日志（Planner 理由高亮）；产出 `data/articles/<assistant_id>/*.md`；**三个测试（Skill 加载、工具注册、记忆隔离）随 MVP 交付**。
- **阶段 3**：`memory/store.py` 的 `recall` 升级为 FTS5 trigram 检索（隔离语义已在 MVP 就位）；APScheduler 由 Runtime 持有并在 `schedule` 长驻命令中挂载到同一事件循环，job 绑定助手。
- **阶段 4**：EventBus 增加 SSE 订阅者；Web UI 为 VS Code 式文档优先工作台，含助手选择器、项目导入/资源树、多标签编辑器、Markdown 预览和项目 Agent 面板。选区改写与聊天修改统一输出 change set；Runtime 新增受控编辑入口，但必须复用既有锁、事件、助手和 MemoryStore 语义，不另建平行业务链路。

---

**请评审**。确认后我进入阶段 2，交付可运行的 MVP 全部代码。
