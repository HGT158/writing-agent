# 阶段 3 代码审查报告

> 审查对象：`writing-agent/` 阶段 3（Memory 充实 + Scheduler）全部新增/改动源码（`memory/store.py`、`memory/short_term.py`、`scheduler/`、`config/settings.py`、`agent/runtime.py`、`agent/__main__.py` 及 4 个新测试文件），并对阶段 2 已修复项做回归确认
> 审查日期：2026-08-07
> 审查方式：逐文件走读 + APScheduler 内部实现源码核对 + 边界输入动态复现 + `pytest tests/ -v` 实测（未改动任何项目代码）
> 环境：Windows 11 / Python 3.13.14（conda `writing-agent`）/ pytest 9.1.1 / langgraph 1.2.10 / APScheduler 3.11.3 / openai 2.53.0

---

## 总体评价

先说做得好的部分：

- **FTS5 trigram 迁移工程质量高**：`PRAGMA user_version` 版本化 + "虚拟表缺失 / tokenizer 非 trigram / 触发器不齐"三种不完整状态全部触发单事务重建回填，且每种状态都有对应测试（`test_memory_fts.py` 5 例）。外部内容索引 + 六个触发器保证增删改同步，`assistant_id UNINDEXED` 列让 MATCH 结果 join 回原表后仍能强制隔离过滤。
- **Scheduler 生命周期正确且有硬证据**：读 APScheduler 3.11.3 源码确认 `AsyncIOExecutor.shutdown` 会 cancel 所有未完成 job task，`RuntimeScheduler.shutdown` 再 gather 等待 `finally` 清理——"停 Scheduler → 等 job 释放 run_locks → 关 MCP/Store"的顺序与架构 §5.8 完全一致，并有 `test_shutdown_releases_active_job_run_lock_before_store_close` 用真实 SQLite 锁断言。
- **注册容错到位**：job 四字段校验 + 助手存在性校验，坏配置只记 warning 不阻断（架构 §5.8 要求），`coalesce=True`/`max_instances=1`/`misfire_grace_time=60` 防积压补跑；`run_scheduled_job` 区分"助手正忙（跳过）"与"其他异常（记失败）"，`AssistantBusyError` 不会被误当失败。
- **跨任务记忆延续有真图级验证**：`test_memory_carryover.py` 用真实 `AgentRuntime` 连跑两次任务，断言第二次 Planner prompt 中同时出现第一次沉淀的偏好与文章索引——验收标准"第二次同主题可读到第一次沉淀"有可执行证据。
- **阶段 2 全部 26 项修复无回归**：reflect→observe 回边、锁原子占位/所有权、sources 回读、Skill 去重、json 模式回退收窄等关键修复逐一核对在位，39/39 全绿。
- **依赖版本收敛**：`APScheduler>=3.10,<4` 显式挡住 4.x 破坏性 API 变更，requirements 注释清晰。

本阶段未发现 P0 级问题。发现 **1 个 P1**（短 token 任务描述的检索静默失效）与 **7 个 P2**，均附复现证据或代码依据。

测试现状：`pytest tests/ -v` → **39 passed**（2.43s），红线 `test_memory_isolation.py` 6 例全绿。

---

## P0 — 无

核心循环、锁机制、调度生命周期均验证有效，未发现必须阻断验收的缺陷。

---

## P1 — 应修复（检索可用性）

### 1. 短 token 任务描述触发整句短语回退，FTS 检索静默失效

**位置**：`memory/store.py` `_fts_query` L28-47（回退分支 L45-46）、`recall` L95-102

`_fts_query` 用 `[\w一-鿿]+` 抽取连续词元，只保留长度 ≥3 的词元（中文切三字窗口）。当任务描述由空格分隔的短词组成、**所有词元都不足 3 字**时，`terms` 为空，回退为把**整句**当一个带引号的 FTS 短语：

```
"写 AI 文章"  → terms 全被跳过 → MATCH '"写 AI 文章"'
```

trigram 短语要求**精确子串**匹配，历史内容里几乎不可能出现一字不差的整句，于是 messages/articles 两路检索全部空手而归，而 `recall` 不会报错——只剩 profile 全文和 `recent_articles` 兜底的最近 3 篇文章。

**复现证据**（临时库先存消息"写一篇AI文章，介绍模型蒸馏"与文章《模型蒸馏入门》，只读脚本实测）：

```
recall("tech-writer", "写 AI 文章")
  match_query='"写 AI 文章"'
  历史消息命中：否（FTS/LIKE 均未命中，仅 recent_articles 兜底）
```

同型输入还有"生成 AI 日报"、"AI 新闻"等——这类简短任务在日用场景中相当常见。阶段 3 验收（同主题第二次读到偏好与文章）靠 profile 恒注入 + recent_articles 仍可达成，但**消息级检索这条腿在此类输入上整体失效**，且无任何日志提示。架构 §5.7 承诺的"保证 recall 不静默漏检"目前只覆盖了 `len(query) < 3` 一支。

**修复建议**：词元抽取为空（或只剩 <3 字词元）时，改为对这些短词元做 OR LIKE（如"写/AI/文章"→ 三个 `LIKE '%…%'`），或对 2 字中文词做滑窗 LIKE；至少记一条 debug 日志标明本次检索走了降级路径。补一条"短 token 查询仍可召回相关消息"的回归测试。

---

## P2 — 建议改进（健壮性、开箱体验与工程卫生）

### 2. 内置 JOBS 指向不会自动创建的 tech-writer，全新环境 schedule 空转

**位置**：`config/settings.py` L12-19、`scheduler/scheduler.py` `_register` L43

`AssistantRegistry` 只自动创建 `default` 助手。全新 `data/` 目录下运行 `python -m agent schedule`，内置的 `daily-ai-news` 因 `tech-writer` 不存在被跳过，调度器以 0 个 job 启动（有 warning，不算静默失败，但开箱即空转）。阶段 2 审查 P2-20 的修复注释是"阶段 3 生效"，如今生效后默认配置仍指向不存在的助手。

**复现证据**（临时 data 目录 + 真实 AssistantRegistry + 内置 JOBS）：

```
可用助手=['default']，注册成功 jobs=[]，
警告=["定时任务 daily-ai-news 未注册：'助手不存在：tech-writer。可用助手：default'"]
```

**修复建议**：JOBS 改指向 `default`（README 示例同步），或注册失败时回退 default 并在 warning 中说明。

### 3. recall 全链路无防御，检索异常会以裸 traceback 打断任务

**位置**：`memory/store.py` `recall` L83-113、`agent/__main__.py` `_cmd_run` L50-58

`recall` 内的 FTS MATCH、LIKE、profile.md 读取均无 try/except。一旦抛出（如 `sqlite3.OperationalError`、profile 被手工改成非法编码时的 `UnicodeDecodeError`），异常穿过 `runtime.run` 直达 CLI，而 `_cmd_run` 只捕获 `AssistantBusyError / KeyError / RuntimeError`——用户看到裸 traceback，任务以非预期方式终止。

当前 `_fts_query` 的构造是安全的（见附录：10 组边界输入无一触发异常），但实验证明 FTS 层**确实会**对畸形 MATCH 抛 `OperationalError`：

```
MATCH 'OR'       → sqlite3.OperationalError: fts5: syntax error near "OR"
MATCH '"ab" OR'  → sqlite3.OperationalError: fts5: syntax error near ""
MATCH 'col:xxx'  → sqlite3.OperationalError: no such column: col
```

即这道防线目前完全依赖 `_fts_query` 永远正确——未来任何改动（包括架构 §5.7 预留的语义检索）引入非法表达式都会直接炸在任务入口。

**修复建议**：`recall` 内部对检索段 try/except，异常时降级为"profile + 最近文章"+ warning 日志，保证检索故障永不阻断写作任务；`_cmd_run` 可顺带补一个兜底 `Exception` 分支转 failed 事件。

### 4. LIKE 回退未转义 `%`/`_` 通配符

**位置**：`memory/short_term.py` `search_messages` L187-193、`search_articles` L220-225

短查询 LIKE 路径直接拼 `%query%`，查询中的 `%`/`_` 被当作通配符。实测查询 `a_` 能召回含 `xab` 的消息（`_` 匹配了任意字符）。语义偏差轻微、无安全问题，修复成本低：参数加 `ESCAPE '\'` 并对查询做转义。

### 5. `_fts_query` 16 词上限取头部，长任务尾部关键词不参与检索

**位置**：`memory/store.py` `_fts_query` L41-44

词元上限 16 个，长任务描述（如带多节要求的复杂指令）只有前段词元进入查询，尾部关键词无法命中。影响有限（BM25 排序 + recent_articles 兜底），建议改为按词元位置均匀采样，或在文档中注明该取舍。

### 6. 文档同步：AGENTS.md 版本与数量描述已过时

**位置**：`AGENTS.md` L10、L57

- "阶段 1 架构：✅ 完成，文档 `docs/phase1-architecture.md` **v1.4**"——文档实际已升 **v1.5**（阶段 3 实施约定）；
- 代码地图"`docs/` phase1-architecture.md（v1.4）+ 三份审查报告"——实际是两份审查报告（phase1-architecture-review、phase2-code-review）；
- "当前状态（2026-08-06）"日期可顺带更新。

README 与架构文档版本一致（均 v1.5），无需改动。

### 7. `_cmd_run` 的 `runtime.start()` 在 try 之外

**位置**：`agent/__main__.py` L46-48

`await runtime.start()` 若抛异常（如 MCP 启动中意外错误），不会进入 `finally` 的 `runtime.close()`——store 连接与已部分启动的 MCP 资源不清理。进程随即退出，实际危害小，但与 `_cmd_schedule`（start 在 try 内）不一致。建议把 start 也纳入 try/finally。

### 8. openai SDK 版本范围过宽

**位置**：`requirements.txt` L2

`openai>=1.50` 无上限，实测环境装的是 **2.53.0**（1.x→2.x 有破坏性变更）。当前代码在 2.x 下测试全绿，但按 1.5x 新装与按 2.x 新装得到的是两代 SDK。阶段 2 审查 P2-18 收紧了 langgraph 下限，此处建议同样处理：`openai>=2.0,<3`（若确认放弃 1.x 支持）。

---

## 测试覆盖缺口

现有 39 个测试对阶段 3 新机制（FTS 迁移三态、锁隔离/回收、调度注册/派发/关停、跨任务延续）覆盖扎实，以下为新增缺口：

- `recall` 短 token / 词元抽取为空的降级路径（对应 P1#1，本次复现脚本可直接改造成回归测试）；
- `recall` 内部异常的降级行为（对应 P2#3，可用损坏的 profile 文件或注入坏 MATCH 构造）；
- JOBS 引用不存在助手的注册告警（`test_scheduler.py` 已有近似用例，但建议用真实 `AssistantRegistry` 走一遍开箱场景）；
- 空 JOBS 列表启动/关闭调度器（边界，成本低）。

另：`test_scheduler.py` 六个用例均自建 `_FakeRuntime`，与真实 `AgentRuntime` 的契约（`bus`/`assistants`/`run` 签名）靠手工对齐；阶段 4 Web 层复用 Runtime 前可考虑抽一个最小 Protocol，防止 fake 漂移。

---

## 备忘（不构成问题）

- **messages 表每会话仅 2 行**（任务启动的 user 消息 + 定稿后的 assistant 消息），messages_fts 素材天然偏薄，跨任务延续主要靠 profile + articles。这是当前设计取舍，阶段 4 若要 richer 的对话记忆需在此扩展。
- **misfire_grace_time=60**：电脑休眠错过触发超 60 秒则跳过本次、不补跑——与 coalesce 的防积压意图一致，README 未明说，可在 Scheduler 小节补一句。
- `D:\test_agent\pytest-temp-stage3-review` 是 08-07 某次 pytest 运行遗留的临时目录（在项目目录之外，当前权限不可读），可人工清理。
- 项目仍未初始化 git 仓库；`.gitignore` 已在阶段 2 备好，建仓时可直接启用。

---

## 修复优先级建议

1. **P1#1 优先**：改动集中在 `_fts_query` 回退分支（约 10 行）+ 一个回归测试，直接决定阶段 3 记忆延续在常见短任务输入下的实际体验。
2. **P2#2 + P2#6 顺手一起**：JOBS 指向与 AGENTS.md 版本描述，纯配置/文档修正。
3. **P2#3 建议与 P1#1 同批**：两者都在 `recall` 周边，一次补齐"检索质量 + 检索不阻断"两条保证。
4. 其余 P2 按清单顺序消化即可，均不阻塞阶段 3 验收。

---

## 附：审查中使用的验证手段

- `C:\miniconda\envs\writing-agent\python.exe -m pytest tests/ -v` → **39 passed**（2.43s，含红线 test_memory_isolation 6 例）
- 只读复现脚本（置于审查方工作目录，项目零改动）：
  - 10 组边界查询跑 `_fts_query` + `recall`：纯标点、纯 emoji、引号、`C++`、`%`、FTS 关键字（OR/AND/NOT）、300 字符长串——无一触发异常，构造器 escaping 有效；
  - 短 token 查询召回质量实测：确认"写 AI 文章"无法召回"写一篇AI文章…"（P1#1 证据）；
  - 直接向 `messages_fts` 注入畸形 MATCH（`OR` / `"ab" OR` / `col:xxx`）：确认失败形态为 `sqlite3.OperationalError`，不在 CLI 捕获列表（P2#3 证据）；
  - LIKE 通配符实测：`a_` 召回 `xab`（P2#4 证据）；
  - 全新 data 目录 + 内置 JOBS：0 job 注册 + warning（P2#2 证据）。
- APScheduler 3.11.3 `executors/asyncio.py` 源码核对：`AsyncIOExecutor.shutdown` cancel 未完成 task，与 `RuntimeScheduler.shutdown` 的 gather 等待共同实现"取消 → 等 finally → 释放锁"（架构 §5.8 承诺成立）。
- 阶段 2 审查报告 26 项修复逐一对照源码确认在位（reflect→observe 回边、INSERT OR IGNORE + row-None 重试、release 带 task_id、sources 回读、Skill 去重、`finalize_article_impl` 结构化、id 正则校验、json 回退收窄 BadRequestError、MCP 容错、idempotent 重试等）。

---

## 处理结果（2026-08-07）

本报告列出的 1 个 P1 与 7 个 P2 均确认存在，已全部修复；未进入阶段 4。

| # | 结论 | 处理结果 | 回归证据 |
|---|------|----------|----------|
| P1-1 | 存在 | 无长度至少 3 的词元时不再构造整句 FTS 短语，改为按短词元执行 OR LIKE 降级 | `test_short_tokens_fall_back_to_like_search` |
| P2-2 | 存在 | 内置 `daily-ai-news` 改绑自动创建的 `default` 助手，全新数据目录可直接注册 | `test_load_settings_copies_configured_jobs` |
| P2-3 | 存在 | profile、两路 FTS、两路 LIKE、最近文章查询分别隔离异常并 warning；CLI 将未预期异常转为 failed 事件与退出码 2，且始终关闭 Runtime | `test_invalid_profile_encoding_does_not_block_database_recall`、`test_fts_failure_falls_back_to_recent_articles`、`test_cli_converts_unexpected_run_error_and_closes_runtime` |
| P2-4 | 存在 | LIKE 降级按空白保留完整 token，再对 `\\`、`%`、`_` 做字面量转义，并在 SQL 中显式声明 `ESCAPE '\\'` | `test_like_fallback_treats_wildcards_as_literals`（覆盖 `_`、`%`、纯 `%`、反斜杠） |
| P2-5 | 存在 | FTS 词元达到上限时改为覆盖整个候选序列的均匀采样，尾部主题词参与检索 | `test_long_query_includes_topic_terms_from_the_tail` |
| P2-6 | 存在 | `AGENTS.md` 日期、架构版本、测试数量与文档地图同步为 2026-08-07 / v1.6 / 49 | 全量测试与文档核对 |
| P2-7 | 存在 | `_cmd_run` 从 `runtime.start()` 起进入 `try/finally`，启动失败也执行 `runtime.close()` | `test_cli_closes_runtime_when_start_fails` |
| P2-8 | 存在 | OpenAI SDK 依赖收紧为 `openai>=2.0,<3` | 当前环境 OpenAI 2.53.0；`pip check` 无冲突 |

同步更新：架构文档升至 v1.6；README 说明默认定时助手与 `misfire_grace_time=60` 的不补跑语义；`requirements.txt` 明确 OpenAI 2.x 支持范围。

最终验证：

- 红线 `tests/test_memory_isolation.py`：**6 passed**。
- 全量 `pytest tests/ -v`：**49 passed**。
- 依赖一致性 `python -m pip check`：**No broken requirements found**。

---

## 复审（第二轮，2026-08-07）

验证方式：全部改动文件重读 + 项目指定环境跑 `pytest tests/ -v` → **49 passed**（4.34s，红线 6 例全绿）+ 独立只读复现脚本 17 项逐条验证 + `pip check` 无冲突 + 架构文档 v1.6 / README / AGENTS.md 一致性核对。**未改动任何代码。**

### 修复确认：8 项全部有效

| # | 复核结论 | 关键证据 |
|---|----------|----------|
| P1-1 短 token 检索失效 | 已修复 | `_fts_query` 抽取为空时返回空串，`recall` 改走按空白词元的转义 OR LIKE；独立复现"写 AI 文章"成功召回历史消息"写一篇AI文章，介绍模型蒸馏"（修复前实测不命中）；回归测试 `test_short_tokens_fall_back_to_like_search` 在位 |
| P2-2 JOBS 指向不存在的助手 | 已修复 | `JOBS` 改绑 `default`；独立复现全新 data 目录注册 jobs=['daily-ai-news']（修复前实测 0 job + warning）；README 两处同步说明 |
| P2-3 recall 无防御 | 已修复 | profile（OSError/UnicodeError）、两路 FTS、两路 LIKE、最近文章六路分别 try + warning；坏 profile 字节（\xff\xfe\xfa）与 DROP 掉 articles_fts 两个场景实测均降级不阻断；CLI 补 `except Exception` 兜底转 failed 事件 + 退出码 2，且正确不吞 KeyboardInterrupt/SystemExit；`runtime.start()` 已纳入 try/finally |
| P2-4 LIKE 通配符未转义 | 已修复 | `_like_patterns` 按 `\`→`%`→`_` 顺序转义，SQL 显式 `ESCAPE '\'`；独立复现 `a_` 只命中字面量、不再误扩 `xab`；参数化回归测试覆盖 4 种通配符场景 |
| P2-5 16 词上限取头部 | 已修复 | 改为全候选均匀采样（`index*(len-1)//(cap-1)`，首尾均含）；独立复现长查询尾部主题词"量子计算"进入 MATCH 且词元数恰为 16，端到端召回成功 |
| P2-6 文档过时 | 已修复 | AGENTS.md 同步为 v1.6 / 2026-08-07 / 49 测试；"三份审查报告"与实际 docs 目录一致 |
| P2-7 start 在 try 之外 | 已修复 | `_cmd_run` 从 `start()` 起进入 try/finally；回归测试 `test_cli_closes_runtime_when_start_fails` 断言关闭行为 |
| P2-8 openai 版本过宽 | 已修复 | `openai>=2.0,<3`，与实测环境 2.53.0 一致，`pip check` 干净 |

修复质量整体高于预期：recall 的六路隔离粒度比建议更细；LIKE 转义回归测试带反例断言（确认"不误扩"而非只确认"能命中"）；架构文档 §5.7/§5.8/§9 与 README 速查表同步无遗漏。

### 新发现问题（本轮修复引入）

**R1（P2）｜LIKE 降级路径的模式数无上限**

**位置**：`memory/store.py` `_like_patterns` L56-64

对每个空白分隔词元生成一个 LIKE 模式，去重但无数量上限（FTS 路径有 cap=16，此处没有）。实测 50 个互不相同的短词元生成 50 个 OR LIKE 子句。该路径只在"无任何 ≥3 字词元"时触发，日常短任务词元很少、个人数据规模下无实际危害；极端输入下是一长串无索引全扫描，且六路容错会把真正的 SQL 错误降级为空检索而非崩溃。建议顺手给 `_like_patterns` 加与 FTS 一致的采样上限（如 16 个模式），让两条降级路径同构。

### 备忘（不构成问题）

- 长短混合查询中短词元不参与检索（如"写 AI 文章 模型蒸馏"只用"模型蒸馏"的 trigram）：架构 §5.7 明示的取舍，trigram 通常足以命中相关内容，无需处理。
- `python -m agent run` 执行中 Ctrl+C 会冒裸的 KeyboardInterrupt traceback（schedule 分支已处理，run 分支没有）：阶段 2 遗留行为，与本轮修复无关，可在阶段 4 前顺手补上。

### 结论

8 项修复全部成立且均带回归测试，49/49 全绿，无 P0/P1 回归。仅 R1 一个低危同构性建议，不阻塞收口。阶段 3 审查闭环，等待用户确认后可进入阶段 4。

---

## 第二轮复审处理结果（2026-08-07）

- **R1 确认存在并已修复**：`_like_patterns` 增加与 FTS 一致的 16 项上限；去重和字面量转义后若超限，在全部模式中均匀采样并保留首尾，避免极端短词任务生成无上限 OR LIKE 子句。
- **TDD 证据**：新增 `test_like_fallback_caps_patterns_and_keeps_tail_terms`。修复前 50 个短词元产生 50 个模式，断言 `50 == 16` 失败；修复后模式数为 16，且首尾词元均保留。
- **备忘不纳入本轮**：`run` 模式 Ctrl+C 的 KeyboardInterrupt 展示属于阶段 2 遗留且原复审明确标为“不构成问题”，本次不扩展阶段 3 范围。
- **架构同步**：`docs/phase1-architecture.md` 升至 v1.7，明确 FTS 与 LIKE 两条检索路径均采用最多 16 项的全序列均匀采样。

最终验证：红线 `tests/test_memory_isolation.py` **6 passed**；全量 `pytest tests/ -v` **50 passed**。阶段 3 保持完成状态，未进入阶段 4。
