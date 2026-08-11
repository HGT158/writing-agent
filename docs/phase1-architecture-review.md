# 阶段 1 架构文档审查报告

> 审查对象：`docs/phase1-architecture.md`（v1.1 · 2026-08-05）
> 审查日期：2026-08-06
> 结论：**整体设计质量较高，可进入阶段 2，但建议先修复 4 个 P0 问题**，否则 MVP 实现时会被迫返工或违背自身设计原则。

---

## 总体评价（先说优点）

文档结构完整、决策可追溯：先给结论再给理由（第 1 节决策表）、有替代方案对比（第 7 节）、错误处理与边界有专节（第 9 节）。「错误即观察」「Planner 选择理由必填可观测」「store 层强制 `assistant_id` 为第一位置参数」「Skill 渐进式披露」这几个设计都是亮点，尤其多助手隔离方案（第 4 节）从类型签名层面防串记忆，思路正确。

以下按严重程度分级列出问题。

---

## P0 — 必须修复（逻辑不自洽或违背自身设计原则）

### 1. 「降级自由文本规划」的兜底路径不自洽（§5.1、§9）

Planner 输出非法 JSON 时的策略是：重试 1 次 → 仍失败则「降级自由文本规划」。但状态机的条件边依赖 `state.plan.next_action`（`call_tool` / `activate_skill` / `write` / `finish`）来路由——自由文本无法解析出 `next_action`，状态机拿到一个没有合法路由的 Plan，Loop 直接卡死。兜底必须定义一个**可路由的出口**，例如：降级时强制生成 `next_action="finish"` 并附原因、或标记 `status=failed` 终止并落日志，而不是一个无法驱动状态机的中间态。

### 2. 状态图与路由描述不一致（§3 状态图 vs §3.2）

§3.2 明确说「路由由 Planner 节点决定：条件边读取 `state.plan.next_action`」，这意味着 Plan 节点出边应分支到 Act（call_tool/activate_skill）、Write（write）、Done（finish）四处。但 §3 的状态图中 Plan 的唯一出边是 Act，`write` 只能经 Reflect 进入、`finish` 也只画在 Reflect 之后。图与文矛盾，实现者无法判断条件边到底挂在 Plan 还是 Reflect。建议重画状态图，补齐 Plan→Write、Plan→Done 边，或修改 §3.2 的措辞使两者一致。

### 3. API 缺省行为违反自己定义的隔离原则（§5.9 vs §4.3）

§4.3 隔离规则表明确写了文章索引「跨助手 ❌ 查询不到」，但 §5.9 定义 `GET /api/articles?assistant_id=X` 为「缺省返回全部」。虽然单用户拥有全部助手、这不算安全漏洞，但它与文档自身的隔离红线（以及 `test_memory_isolation.py` 要守护的语义）直接冲突。建议改为 `assistant_id` 必填，或明确写清「API 层允许跨助手列举是管理视角的例外」并说明理由，二选一，不能含糊。

### 4. 同一助手并发运行无控制设计（全文缺失）

Scheduler 定时任务、CLI、API 三个入口都可能让**同一个助手同时跑两个 Agent Loop**（例如早 8 点定时任务未跑完时用户又提交了任务）。此时：

- `profile.md` 的增量改写会发生写竞争（Markdown 文件无任何锁/串行机制）；
- 两个 Loop 可能同时写 `data/articles/<id>/` 甚至同名文件；
- SQLite 有自身并发处理，但两个 Loop 对同一会话上下文的 checkpoint 写入语义未定义。

建议补一节并发策略：最简方案是「同一 `assistant_id` 同时只允许一个运行中任务，后来者排队或拒绝」，在 Runtime 层加一把按助手粒度的内存锁即可，成本极低。

---

## P1 — 设计缺口（建议评审通过前补充）

### 5. 内置工具与 filesystem MCP server 职责重叠，且无沙箱（§5.4、§5.6）

内置工具已有 `save_markdown` / `read_file`，同时又注册了 filesystem MCP server（可读写整个 `data/` 目录），但全文没有说明 filesystem MCP 的用途。两条写文件路径并存会带来问题：Planner 可能用 filesystem MCP 的 `write_file` 直接写文章，**绕过 `articles` 索引表**，导致文章管理功能看不到这篇文章；`read_file` 也没有定义路径沙箱（是否限制在 `data/` 内）。建议：明确 filesystem MCP 的定位（如果仅为读写 data 目录，内置工具已覆盖，可移除）；给内置文件工具定义路径白名单。

### 6. Skill 依赖工具缺失时的边界未覆盖（§5.5、§9）

§9 规定 MCP server 启动失败时该工具不注册、不阻断启动；§5.5 规定 Skill 通过 `tools.yaml` 声明依赖（research 依赖 `tavily_search` / `fetch`）。那么当 Tavily 未注册而 Planner 激活 research skill 时，应发生什么？（激活失败并告知 Planner？降级为无工具检索？）这是必然会遇到的组合场景，§9 的边界表应补一行。

### 7. 长 Loop 上下文裁剪策略缺失（§3.1）

`messages: list[BaseMessage]` 无界增长，25 步上限只防死循环、不防 token 爆炸：每轮 Planner 注入（人设 + Skill 清单 + 工具 schema + 记忆摘要）+ 搜索结果全文进上下文，十几轮后极易超模型窗口或成本失控。建议补充裁剪策略：Observation 只存结构化摘要（当前定义是对的，但要明确「不把工具原始输出全量进 messages」）、历史观察滑窗、或超限时强制摘要压缩。

### 8. 助手删除后的孤儿数据策略缺失（§4.2）

「增删文件夹即增删助手」，但删除助手目录后，SQLite 里该助手的 `sessions` / `messages` / `articles` 行全部变成孤儿数据，`data/articles/<id>/` 目录也会残留。应定义清理策略：删除时归档/级联清理，或至少在文档中声明「SQL 数据保留、仅删配置」的语义。

### 9. 中文检索方案未落地（§5.7 vs §7）

§7 说「FTS5 全文检索免费获得」，§5.7 说阶段 3 用「关键词 LIKE 检索」。两者对中文都不友好：LIKE 只能整串匹配且全表扫描；FTS5 默认 unicode61 分词器对中文基本无效，需要 trigram tokenizer 或外接中文分词（如 jieba）。这是阶段 3 `recall` 质量的核心，建议现在就定方案，避免阶段 3 返工。

### 10. tests/ 未列入阶段 2 交付范围（§8）

§8 用 ★ 标注阶段 2 交付内容，但 `tests/`（含 `test_memory_isolation.py`）没有 ★；而文末和 §8 注释都强调该测试是「多助手架构的核心回归测试」。隔离语义既然在 MVP 就位，测试也应进 MVP，否则「store 层强制 assistant_id」的承诺没有验证手段。建议把三个测试文件标 ★。

---

## P2 — 细节与一致性问题

11. **一次性 CLI 任务的 `session_id` 生成规则未定义**（§3.1、§5.3）：checkpoint 的 `thread_id = <assistant_id>:<session_id>` 依赖它，CLI 单发任务每次是新会话还是可续接？需一句话定义。
12. **文章同名冲突策略缺失**：同一助手两次写同标题文章，`data/articles/<id>/模型蒸馏.md` 会互相覆盖，建议加时间戳或序号后缀。
13. **Reflect 质检标准未定义**：`done_criteria_met` 的判断依据完全交给 LLM 自由心证，建议给 Reflect prompt 一个明确 checklist（来源数量、覆盖度、引用可追溯等），否则容易过早收敛或反复循环。
14. **模块归属缺口**：write 节点（分段成文编排，逻辑量不小）与 observe/reflect 节点没有指明落在哪个模块；内置工具（save_markdown 等）在目录结构中没有载体模块（如 `agent/tools.py`）；`agent/` 包缺 `__main__.py`，而 CLI 入口是 `python -m agent`。
15. **`${TAVILY_API_KEY}` 环境变量插值**：官方 Claude Desktop 配置格式**不支持** `${VAR}` 展开，这是 registry.py 需要自实现的能力。文档称「格式与 Claude Desktop 配置兼容」，建议注明这一超集差异，避免迁移误解。
16. **配置示例硬编码绝对路径**：`mcp_servers.json` 示例中 `D:/test_agent/writing-agent/data` 是本机绝对路径，示例应改为相对路径或占位符。
17. **A5 表述与依赖现实有出入**：「只用 langgraph + langchain-core」，实际还需要 `langgraph-checkpoint-sqlite`（及 aiosqlite）。不算错误，但建议更新措辞，免得阶段 2 对 requirements 时产生疑惑。
18. **recall 时机表述不一**：§6.1 时序图是 Loop 开始前 recall 一次；§5.7 表格写「Planner 每轮注入摘要」。二者需统一（例如「启动时 recall 一次，摘要随 state 每轮注入」）。
19. **`finalize_article` 与 `save_markdown` 职责区分不清**：两个内置工具听起来都是写文件，需一句话说明差异（如 finalize 负责写文件 + 登记 articles 索引 + 触发 memorize）。
20. **`memorize` 接口签名不完整**：`recall(assistant_id, query) -> str` 定义完整，memorize 只有 `memorize(assistant_id, ...)`，建议补全参数（写入类型、内容等）。
21. **代码包 `assistants/` 与数据目录 `data/assistants/` 同名**：容易混淆，代码包可考虑改名（如 `assistant_registry/` 或并入 `agent/`）。
22. **Mermaid 小风险**：§4.4 中 `P1 -.-|互不可见| P2` 的无箭头虚线带标签语法在部分渲染器上不稳定，建议改为 `P1 -.->|互不可见| P2`。

---

## 建议的处理顺序

先修 P0 的 1、2、3（都是改文档措辞/补一小节，半小时内可完成），P0-4 与 P1 各项在 v1.2 中补齐后即可进入阶段 2；P2 可在阶段 2 实现过程中顺手消化。

---

## 处理结果（2026-08-06，架构文档 v1.2 已修复全部 22 项）

**判定：22 条全部成立，无误报。** 修复落点：

| # | 判定 | 修复方式（v1.2 落点） |
|---|------|----------------------|
| P0-1 | 成立 | 降级路径改为强制构造可路由的 finish/failed，删除"自由文本规划"中间态（§5.1、§9） |
| P0-2 | 成立 | 状态图重画：Plan 出边分支到 Act/Write/Done，并明确条件边挂在 Plan 与 Reflect 之后（§3） |
| P0-3 | 成立 | 采用方案一：`assistant_id` 必填，缺省返回 400，API 不提供跨助手列举（§5.9） |
| P0-4 | 成立 | 新增 §4.6：per-assistant asyncio 锁，后来者拒绝（API 409 / CLI 报错 / Scheduler 跳过）；SQLite 开 WAL |
| P1-5 | 成立 | filesystem MCP 移出默认配置并说明理由；内置文件工具沙箱限 `data/`（§5.4、§5.6） |
| P1-6 | 成立 | 激活前校验依赖，缺失则激活失败返回 Observation 由 Planner 决策（§5.5、§9） |
| P1-7 | 成立 | 新增 §3.3 上下文裁剪策略：Observation 摘要化、8 条滑窗、60k 强制压缩、正文不进循环上下文 |
| P1-8 | 成立 | 删除语义定为"默认归档到 data/archive/，SQL 行保留不可见，--purge 才级联删除"（§4.2） |
| P1-9 | 成立 | 中文检索定案 FTS5 trigram（SQLite ≥3.34 内置），放弃 unicode61 与 LIKE（§5.7） |
| P1-10 | 成立 | tests/ 三个文件全部标 ★，随 MVP 交付（§8、§10） |
| P2-11 | 成立 | CLI 每次运行生成 uuid4 新会话，`--resume <session_id>` 续接（§3.1、§4.5） |
| P2-12 | 成立 | `finalize_article` 自动加时间戳后缀防覆盖（§8 注 2、§9） |
| P2-13 | 成立 | 新增 §3.4 Reflect 质检 checklist，连续 3 次未过强制 finish 并标注存疑项 |
| P2-14 | 成立 | write 节点与五节点装配归 `loop.py`；内置工具归 `agent/tools.py`；补 `agent/__main__.py`（§5.3、§5.4、§8 注 1） |
| P2-15 | 成立 | 注明 `${VAR}` 与 `${PROJECT_ROOT}` 插值是相对 Claude Desktop 格式的超集扩展（§5.6） |
| P2-16 | 成立 | 配置示例改为占位符/相对路径，filesystem 示例移除（§5.6） |
| P2-17 | 成立 | A5 措辞更新为 langgraph + langchain-core + langgraph-checkpoint-sqlite(+aiosqlite)（§0） |
| P2-18 | 成立 | 统一为"启动时 recall 一次，存 state.memory_context，Planner 每轮从 state 注入"（§3.1、§5.7、§6.1） |
| P2-19 | 成立 | finalize_article = 写文件+登记索引+触发 memorize；save_markdown = 纯写中间产物（§8 注 2） |
| P2-20 | 成立 | memorize 签名补全：kind（preference/style/topic/article）+ content + session_id（§5.7） |
| P2-21 | 成立 | 代码模块改为 `agent/assistant_registry.py`，顶层不再有 `assistants/` 包（§8 注 1） |
| P2-22 | 成立 | 改为 `P1 -.->|互不可见| P2`（§4.4） |

---

## v1.2 复审（2026-08-06）

对上表进行了独立逐项复核，确认 22 项修复均真实落地且方向正确（非表面改动）：降级路径、状态图、API 隔离、并发锁、裁剪策略、trigram 定案等都能自圆其说。但细读 v1.2 **新增内容**后，发现 4 个新问题（R1–R4）与 3 个小项（R5），其中 R1 建议在写阶段 2 代码前定案：

### R1. 运行锁跨进程失效（§4.6，最重要）

§4.6 用 `dict[str, asyncio.Lock]` 内存锁，并称"单进程架构（CLI 单发 / FastAPI 单进程）下足够"——这个前提与本架构自身的多入口设计矛盾：CLI 与 FastAPI 是两个独立进程，阶段 3 的 Scheduler 跑在 FastAPI 进程里而用户同时可以开 CLI，甚至两个 CLI 窗口就是两个进程。内存锁互不可见，profile.md 写竞争在这些组合下依然存在。建议改用**跨进程锁**：同一个 app.db 里加一张 `run_locks` 表（`assistant_id` 主键 + `task_id` + 获取时间），插入成功=获锁、任务结束删除行，配合 WAL 天然跨进程，成本与内存锁相当；并顺手定义进程崩溃后残留锁行的清理规则（如按 PID/时间戳回收）。

### R2. `sources` 表被引用但未定义（§3.1、§3.3 vs §5.7）

两处写了"fetch 全文落 SQLite `sources` 表备查"，但 §5.7 的表清单里没有这张表，字段、写入时机都未定义；更关键的是**未说明它是否含 `assistant_id` 列**——它承载任务数据，按第 4 节的隔离红线应当含该列并强制过滤。建议在 §5.7 补一行定义。

### R3. FTS5 trigram 对不足 3 字的查询无效（§5.7）

trigram 分词器要求查询 token **至少 3 个字符**，不足 3 字的 MATCH 无法命中索引。而中文检索词大量是两字词（"风格"、"偏好"、"引用"），恰是 recall 的高频场景。需要为短查询定义回退策略（如对 <3 字查询降级为 LIKE，或查询扩展/补齐），否则阶段 3 的 recall 会静默漏检。

### R4. 内置工具如何获得当前任务的 `assistant_id`（§5.4、§8 注 2）

`finalize_article(title, content)` 签名里没有 `assistant_id`，但它要写 `data/articles/<assistant_id>/` 并登记索引；而工具注册表是**进程启动时一次性构建**的（§5.4 步骤 5），任务却是按助手运行的。工具拿到当前助手上下文的机制（运行时闭包绑定 / 从 state 注入等）需要一句话定义，否则阶段 2 实现时必然卡壳或各写各的。

### R5. 小项

- §3.4"连续 3 次质检未过强制 finish"需要一个计数字段，但 `AgentState`（§3.1）里没有对应项（如 `reflect_fails: int`）。
- §8 `tests/` 行的 ★ 标记不一致（`★ test_skill_loading.py / test_tool_registry.py / ★ test_memory_isolation.py`），虽然 §10 已明确三个测试全部随 MVP 交付，建议标记统一以免误读。
- 归档/删除助手（§4.2）应先检查该助手的运行锁：任务运行中移动目录，在 Windows 上会因文件占用失败。

### 复审结论

R1、R4 影响阶段 2 的代码结构，建议先在文档中定案（各一两句话即可）；R2、R3 可在实现时补，但最好同步进文档；R5 顺手处理。**以上均不构成阻塞——修复确认无误，可进入阶段 2。**

---

## v1.3 处理结果（2026-08-06，R1–R5 全部修复）

**判定：5 条全部成立，无误报。** 修复落点：

| # | 判定 | 修复方式（v1.3 落点） |
|---|------|----------------------|
| R1 | 成立（且为 v1.2 自身引入的缺陷：CLI/FastAPI/多 CLI 窗口是多进程，内存锁互不可见） | 运行锁改为 app.db 内 `run_locks` 表（assistant_id 主键 + task_id + pid + acquired_at）：INSERT 即获锁、结束 DELETE、超 TTL（默认 2h）视为崩溃残留回收；进程内 asyncio.Lock 仅作快路径优化，正确性由 DB 保证（§4.6；§1 决策表、§2 架构图、§5.4、§6.1、§9 同步更新） |
| R2 | 成立 | §5.7 补 `sources` 表定义：含 `assistant_id` + `session_id`，字段 url/title/fulltext/fetched_at，查询强制按 assistant_id 过滤 |
| R3 | 成立 | §5.7 补短查询回退：<3 字查询自动降级 LIKE，防 recall 静默漏检 |
| R4 | 成立 | 统一工具协议定为 `call(args, ctx: ToolContext)`，ctx 携带 assistant_id/session_id/data_dir，由 Executor 调用时注入，不进 LLM 可见的 JSON Schema（§5.2） |
| R5a | 成立 | `AgentState` 补 `reflect_fails: int`（§3.1） |
| R5b | 成立 | §8 tests 行 ★ 标记统一为三个全标 |
| R5c | 成立 | §4.2 补删除前置检查：先查 run_locks，任务运行中拒绝删除；§9 边界表同步加一行 |

### v1.3 复审结论

独立复核与上表一致，5 项修复均真实落地，且质量高于预期：R1 的锁方案比建议更完整（pid + acquired_at + TTL 回收 + 内存锁降级为快路径），全文 §1/§2/§5.4/§6.1/§9 同步无遗漏；R4 的 ToolContext 不仅定义了注入机制，还明确 ctx 不进 LLM 可见的 JSON Schema（杜绝 Planner 伪造归属），比"一句话定案"更周全。

仅剩两个可选级小观察（不阻塞、不强制改文档）：

1. `run_locks.pid` 列只在表结构中出现、用途未定义。实现时可用它做回收前的进程存活校验，或给运行中任务加心跳刷新 `acquired_at`，防极端超长任务（>2h TTL）被误回收。25 步上限下实际任务远短于 2h，风险可忽略。
2. `RUN_LOCK_TTL` 在 §4.6 出现但未列入 `.env.example`（MAX_STEPS 在），阶段 2 顺手补上即可。

**最终结论：通过评审。** 三轮迭代后，文档在设计自洽性、隔离红线、并发安全、错误边界、中文检索方案上均无明显缺口，图文一致、前后呼应。可以进入阶段 2 MVP 实现。
