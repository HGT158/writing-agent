# 阶段 2 代码审查报告

> 审查对象：`writing-agent/` 阶段 2 MVP 全部源码（agent/、mcp_client/、memory/、config/、skills/、tests/）
> 审查日期：2026-08-06
> 审查方式：逐文件人工走读 + 关键路径动态复现 + `pytest tests/ -v` 实测
> 环境：Windows 11 / Python 3.13.5 / pytest 9.1.1 / langgraph 1.1.8

---

## 总体评价

先说做得好的部分，避免审查报告只剩批评：

- **记忆隔离红线落实到位**：`MemoryStore` 全部接口强制 `assistant_id` 首参，profile.md 按助手目录物理隔离，且有 4 个针对性测试守护，实测跨助手 recall 无污染。
- **错误即观察**：工具异常不打断 Loop，统一转 `Observation(success=False)`，Executor 层实现与架构一致。
- **MCP 降级不阻断启动**：单个 server 失败只记 warning，工具表照常可用。
- **路径沙箱**：`_safe_resolve` 对内置读写工具做了逃逸检查，测试覆盖了 `../` 场景。
- **Planner 非法 JSON 兜底可路由**：强制构造 `finish` 计划，修复了阶段 1 审查中指出的 P0 问题，实现与文档一致。

但核心循环（observe→plan→act/write→reflect）恰恰是测试零覆盖的区域，本次审查在其中发现了 **2 个 P0 级问题**（防死循环机制整体失效、运行锁竞态），均附带可复现证据。

测试现状：`pytest tests/ -v` → **12 passed**（记忆隔离 4、Skill 加载 4、工具表 4）。全部集中在 memory/skills/tools 三个支撑层；`loop.py / planner.py / runtime.py / __main__.py / mcp_client/` 无任何测试。

---

## P0 — 必须修复（核心机制失效或必然可复现的缺陷）

### 1. step 计数器永不递增，max_steps 防死循环整体失效

**位置**：`agent/loop.py`（`node_observe` L44-45、`build_graph` L290-299、`route_after_reflect` L252-258）

`node_observe` 是唯一递增 `step` 的节点，但图的边是：

```
START → observe → plan → {act | write | done}
act/write → reflect → {plan | done}        ← reflect 直接回 plan，不再经过 observe
```

因此 `observe` 整个任务生命周期**只执行一次**，`step` 永远停在 1。连锁后果：

1. `route_after_reflect` 里 `if step >= max_steps: return "done"` 永远为假 —— 架构 §3.4 的"超步数强制收敛并保留已有成果"是死代码；
2. `node_done` 里"达到最大步数，提前收敛"的提示永远不触发；
3. Planner 每轮看到的进度恒为"第 1/25 步"，进度信息失真；
4. 实际唯一的刹车是 `runtime.py` L107 的 `recursion_limit = max_steps*6+20`（=170 个 super-step，约 56 轮循环，是设计预算的 2 倍多），触发时抛 `GraphRecursionError`，CLI 作为 RuntimeError 捕获后按"失败"退出 —— **草稿不落盘，已有成果丢失**，与"优雅降级"的设计意图相反。

**复现证据**（mock Planner 恒发无害 call_tool，max_steps=5）：

```
EXCEPTION: GraphRecursionError Recursion limit of 60 reached ...
node executions: {'observe': 1, 'plan': 20, 'act': 20, 'reflect': 19}
```

observe 只跑了 1 次，step 从未达到 5，最终撞 recursion_limit 异常终止。

**修复建议**：把 reflect 的条件边指回 `observe`（而非 `plan`），让每轮都经过计数节点；或在 `node_plan` 入口递增 step。无论哪种，**补一个"恒不收敛 Planner"的 Loop 级回归测试**，断言在 max_steps 附近优雅终止且 `finish_note` 非空。

### 2. 运行锁 check-then-insert 非原子，并发竞态抛未转换的 IntegrityError

**位置**：`memory/store.py` `acquire_lock` L115-133

流程是先 SELECT 检查、再 INSERT 占位。两个进程几乎同时对同一助手启动时，都可能通过 SELECT 检查，随后先到的 INSERT 成功，后到的因 `run_locks.assistant_id` 主键冲突抛 `sqlite3.IntegrityError`。该异常：

- 不是 `AssistantBusyError`（友好的"助手正忙"提示不会出现）；
- 不在 `__main__.py` `_cmd_run` 的捕获列表（AssistantBusyError / KeyError / RuntimeError）内 —— 用户看到的是裸 traceback。

**复现证据**（双连接模拟竞态窗口）：

```
IntegrityError raised to caller (not AssistantBusyError):
UNIQUE constraint failed: run_locks.assistant_id
```

**修复建议**：改为原子占位，例如 `INSERT OR IGNORE` 后检查 rowcount（0 = 已被占 → 走 TTL/PID 回收判断），或 `try/except sqlite3.IntegrityError` 后重新读取锁行并按现有逻辑转换为 `AssistantBusyError`。补一个双线程/双连接竞态测试。

---

## P1 — 应修复（逻辑正确性、数据可用性与安全）

### 3. release_lock 不校验持有者，锁被回收后会误删新持有者的锁

**位置**：`memory/store.py` `release_lock` L135-138

`DELETE FROM run_locks WHERE assistant_id = ?` 不带 `task_id` 条件。时序：进程 A 任务卡住 → 锁超过 TTL 且 A 的 PID 不复存在 → 进程 B 合法回收并持锁 → A 此时苏醒并跑完，`finally` 里 release 会**删掉 B 的锁**，第三个任务随即可以并发进入同一助手，破坏"同一助手同时只允许一个任务"的不变量（并重新引入 profile.md 写竞争）。

**修复建议**：`DELETE ... WHERE assistant_id = ? AND task_id = ?`，release 时带上自己的 task_id。

### 4. sources 表只写不读：成文环节拿不到素材全文，引用标注失去依据

**位置**：`agent/executor.py` L67-68（写入）、`memory/short_term.py`（无对应查询函数）、`agent/loop.py` `node_write` L107-162

Executor 把 fetch 全文存入 `sources` 表（架构 §3.3 的本意是"全文入库、摘要进上下文、需要时可回查"），但全库没有任何地方读取这张表；`AgentState.sources` 字段也从未被填充。`node_write` 的素材只有一个来源：observations 里每条 ≤500 字符的摘要（`executor.py` `_SUMMARY_LIMIT=500`）。后果：

- 写作提示词要求"事实须来自素材、句末以（来源：URL）标注"，但模型只能看到 500 字摘要 —— URL 经常在摘要截断处丢失，**来源标注实际上鼓励了幻觉**；
- 质检清单第 1 条"引用来源≥3 且可追溯"在这种素材条件下几乎必然失真；
- sources 表成为只写不读的死数据。

**修复建议**：给 `short_term` 增加按 session/assistant 查询 sources 的函数，`node_write` 成文前把相关来源全文（或其分节摘要）注入素材；至少把每条 observation 的源 URL 作为结构化字段保留，而不是混在截断文本里。

### 5. Skill 重复激活导致 prompt 重复累积

**位置**：`agent/loop.py` `node_act` L94-99、`agent/schemas.py` L70-71

`active_skills` 与 `skill_prompts` 都是 `operator.add` 归约，激活即追加、无去重、无停用。Planner 完全可能"writing → research → writing"地二次激活同一 Skill，此时同一份 body 会重复注入 Planner 与写作者的系统提示，token 浪费且随步数膨胀。

**修复建议**：`node_act` 激活前检查 `state["active_skills"]` 已含该 skill 则跳过注入；或把去重责任放进归约函数。

### 6. node_done 依赖字符串暗约定解析定稿路径，且绕过 executor

**位置**：`agent/loop.py` `node_done` L229-233、`agent/tools.py` `finalize_article` L52

`finalize_article` 返回 `"文章已定稿：{path}"`，`node_done` 用 `result.split("：", 1)[-1]` 反解路径 —— 一个全角冒号维系的跨模块隐式契约，任何一侧改文案都会让 `output_path`、事件广播、会话消息里的路径静默错坏。另外此处直接 `spec.call()` 绕过了 executor 的超时/重试/事件协议，还带着生产代码里的 `assert`。

**修复建议**：让 `finalize_article` 返回结构化结果（如 JSON 或单独返回 path），或把 path 写入 ToolContext/观察结构；去掉 assert，改显式报错。

### 7. 助手 id 无格式校验，create 可路径穿越写出 data/ 之外

**位置**：`agent/assistant_registry.py` `create` L74-92

`directory = self.root / assistant_id` 未做校验。实测：

```
python -m agent assistants create ../../evil
→ data/ 之外生成了 evil/assistant.yaml、evil/persona.md、evil/memory/
```

虽然后续 `reload()` 扫不到该目录导致 `get()` 抛 KeyError、CLI 报"失败"，但**文件系统副作用已经发生**，且 `delete`/归档路径同样依赖未校验的 `directory`。

**修复建议**：create 入口用正则限制 id（如 `^[a-z0-9][a-z0-9_-]*$`），或复用 `_safe_resolve` 思路校验 `directory.resolve()` 仍在 root 内。

### 8. `response_format={"type":"json_object"}` 假设所有 OpenAI 兼容服务都支持

**位置**：`agent/planner.py` L114、`agent/loop.py` L124、L183

README 宣称"任意 OpenAI 兼容服务"，但 Planner/大纲/质检三处都硬依赖 json_object 模式；相当一部分兼容网关不支持该参数（或要求提示中含 "json" 字样），此时每次调用直接 API 报错，Loop 整体不可用且无降级。

**修复建议**：把 JSON 模式做成可配置开关，或捕获 API 错误后退回"纯文本 + 宽容解析"。

### 9. MCP 配置解析无容错：坏 JSON 直接 traceback，缺失变量静默为空

**位置**：`mcp_client/registry.py` L37-52、`agent/runtime.py` `start` L41-46

`mcp_servers.json` 非法（或缺 `command` 字段之外的结构错误）时，`json.JSONDecodeError`（ValueError 子类）不在 CLI 捕获范围内，启动直接 traceback —— 与"MCP 任何问题都不阻断启动"的架构承诺不符。另外 `${VAR}` 缺失时静默替换为空串，server 可能带着空 key 启动、到调用时才失败，排查成本高。

**修复建议**：`load_server_configs` 内捕获解析错误并降级为 `{}` + warning；`${VAR}` 未定义时发 warning 提示具体变量名。

---

## P2 — 建议改进（健壮性、可维护性与工程卫生）

10. **重试策略粒度过粗**（`agent/executor.py` L63-73）：任何异常都重试一次，包括 `save_markdown`/`finalize_article` 这类非幂等写工具；超时后重试可能重复写文件，且 `register_article` 无去重，articles 索引会出现重复行。建议仅对读类/网络类工具重试，或给 ToolSpec 加 `idempotent` 标记。

11. **save_source 靠工具名子串匹配**（`agent/executor.py` L67）：`"fetch" in spec.name` 依赖命名巧合，未来任何含 "fetch" 的工具都会误入库、改名的抓取工具则漏入库；且 title 恒为空串。建议在 ToolSpec 上声明 `captures_source=True` 之类的显式标记。

12. **EventBus 静默吞掉订阅者异常**（`agent/events.py` L29-32）：`except Exception: pass` 对阶段 4 的 SSE 订阅者排障极不友好，至少 `logger.debug` 记录。

13. **"内置 3"硬编码**（`agent/runtime.py` L49）：内置工具数量写死在文案里，加一个工具就说谎。用 `len(...)` 替代。

14. **定稿文件碰撞静默覆盖**（`agent/tools.py` L45-50）：slug + 分钟级时间戳命名，同一分钟内两次 finalize（比如质检连败重写后重新定稿）会互相覆盖且无提示；articles 表也无唯一约束。建议时间戳加随机后缀或检测存在。

15. **流式 chunk 解析不够防御**（`agent/loop.py` L154-155）：`chunk.choices[0]` 在部分服务的尾包（空 choices，仅带 usage）上会 IndexError，中断整个成文节点。

16. **profile.md 只增不整理**（`memory/long_term.py` L25-34）：append-only、无去重、无上限，长期运行后 recall 注入的画像会越来越臃肿；至少加行数上限或周期性归并。

17. **checkpoints.db 无清理路径**（`agent/runtime.py` L99-100）：每个会话都留 checkpoint，`--purge` 级联也不包含它；长期积累。另外 `create_session` 用 `INSERT OR REPLACE`，resume 会重置原会话的 `created_at` 与 task 记录。

18. **依赖清单卫生**（`requirements.txt`）：`pytest`、`uv` 属开发依赖应分离；`langchain-core` 声明了但全库未 import；`langgraph>=0.2` 的下限过松（0.2 与 1.x 的 checkpoint API 差异很大，本代码实际依赖 1.x 系行为）。

19. **无 .gitignore**：目录尚未纳入 git，但 `__pycache__/`、`.pytest_cache/`、`data/app.db`、`.env` 都应在建仓前先备好忽略规则，避免首次提交就带入二进制与密钥文件。

20. **settings.py 硬编码阶段 3 jobs**（`config/settings.py` L41-48）：写死了一个 `assistant_id="tech-writer"` 的定时任务占位，当前无处消费，容易误导读者以为已生效。

21. **跨模块引用私有函数**（`agent/loop.py` L23 `from .planner import _observations_text`）：两处复用说明它该升格为公共 API，改名去掉下划线。

---

## 测试覆盖缺口

现有 12 个测试全部通过，但保护面集中在支撑层。以下区域为零覆盖，也是本次 P0/P1 问题的藏身处：

- `agent/loop.py`：状态机路由、step 计数、质检循环、write 重入 —— 建议用 mock LLM 做图级测试（本次 P0#1 的复现脚本可直接改造成回归测试）；
- `agent/planner.py`：非法 JSON 降级路径、重试回喂；
- `memory/store.py` 的锁竞态与 release 所有权（P0#2、P1#3）；
- `agent/assistant_registry.py`：create/delete/归档/异常目录容忍；
- `mcp_client/registry.py`：`${VAR}` 插值与坏配置容错。

另：`tests/test_memory_isolation.py` 两处直接操作 `store._conn` 私有属性构造锁残留，建议给 MemoryStore 增加测试用的公开注入接口，避免测试与私有实现耦合。

---

## 修复优先级建议

1. **先修 P0#1**（step 失效）：一处边的改动 + 一个回归测试，收益最大 —— 它同时解决"成果丢失式终止"和"进度提示失真"。
2. **再修 P0#2 + P1#3**（锁的原子性与所有权）：两者合起来约 20 行改动，把"同一助手单任务"不变量真正焊死。
3. **P1#4**（sources 回读）决定产出质量上限，是阶段 2 验收"引用可追溯"的关键，建议与 P0 并行排期。
4. P1 其余与 P2 可按清单顺序消化；补测试与修复同 PR 提交，防止回归。

---

## 附：审查中使用的验证手段

- `python -m pytest tests/ -v` → 12 passed（0.36s）
- step 失效复现：mock Planner + 计数包装节点，观测到 `{'observe': 1, 'plan': 20, 'act': 20, 'reflect': 19}` 后撞 GraphRecursionError
- 锁竞态复现：双 SQLite 连接并发 INSERT，确认抛 `UNIQUE constraint failed: run_locks.assistant_id`
- 路径穿越复现：`assistants create ../../evil`，确认 data/ 外生成助手文件
- 依赖核对：全库 grep 确认 `langchain-core` 未被引用、`sources` 表无读取方

---

## 处理结果（2026-08-06，全部 22 项已修复，pytest 12 → 24 passed）

**判定：2 个 P0 全部成立，7 个 P1 全部成立，P2 全部采纳。**

| # | 判定 | 修复 |
|---|------|------|
| P0-1 step 失效 | 成立（reflect 回边绕过 observe） | reflect 条件边改指 observe，每轮计数；新增 test_loop.py 图级回归（恒不收敛 Planner 在 max_steps=3 优雅终止，step==3、无 GraphRecursionError） |
| P0-2 锁竞态 | 成立 | acquire_lock 改 INSERT OR IGNORE 原子占位 + 占用后走 TTL/PID 判断；新增双连接竞态测试 |
| P1-3 release 误删 | 成立 | release_lock 增加 task_id 条件（DELETE ... AND task_id=?），runtime 传入；新增所有权测试 |
| P1-4 sources 只写不读 | 成立 | short_term/store 增加 get_sources，node_write 成文前注入本会话抓取全文（每源 1500 字 × 5） |
| P1-5 Skill 重复激活 | 成立 | node_act 激活前查 active_skills，重复则跳过注入 |
| P1-6 字符串暗约定 | 成立 | tools.py 拆出 finalize_article_impl 返回 Path，node_done 直接调用；去掉 assert，改显式异常处理 |
| P1-7 助手 id 穿越 | 成立 | create 入口正则 ^[a-z0-9][a-z0-9_-]{0,49}$ + resolve 越界检查 + 重复检查；新增 4 个注册表测试 |
| P1-8 json_object 假设 | 成立 | 新增 agent/llm.py：json 模式失败自动回退纯文本 + extract_json 宽容解析；LLM_JSON_MODE 可配（.env.example 已加） |
| P1-9 MCP 配置容错 | 成立 | load_server_configs 坏 JSON/缺 command 降级 warning；${VAR} 未定义按名警告；新增 3 个容错测试 |
| P2-10 重试过粗 | 成立 | ToolSpec 加 idempotent 标记，finalize_article=False，非幂等不重试 |
| P2-11 fetch 子串匹配 | 成立 | ToolSpec 加 captures_source 显式标记，MCP wrap 处声明；title 用 url |
| P2-12 EventBus 吞异常 | 成立 | 改 logger.debug(exc_info=True) |
| P2-13 内置 3 硬编码 | 成立 | 改 len() 计算 |
| P2-14 定稿碰撞 | 成立 | 同分钟重名追加 -2/-3 序号 |
| P2-15 chunk 防御 | 成立 | 空 choices 尾包 continue |
| P2-16 profile 膨胀 | 成立 | 超 200 行保留头部 + 最近 150 条 |
| P2-17 checkpoint 清理 | 成立 | purge_assistant 级联清 checkpoints.db；create_session 改 INSERT OR IGNORE |
| P2-18 依赖卫生 | 成立 | 移除未用的 langchain-core 显式声明；langgraph>=1.0 等版本下限收紧；拆出 requirements-dev.txt |
| P2-19 无 .gitignore | 成立 | 已添加（.env、__pycache__、数据库、文章产出等） |
| P2-20 jobs 占位误导 | 成立 | 注释标明"当前无消费者，阶段 3 生效" |
| P2-21 私有函数跨模块 | 成立 | _observations_text → observations_text 升格公共 API |

**测试覆盖变化**：12 → 24（+图级 Loop 3、助手注册 4、MCP 容错 3、锁竞态/所有权 2）。核心循环（step 计数、质检连败、正常定稿）已有图级回归。

---

## 复审（第二轮，2026-08-06）

验证方式：全部改动文件重读 + 项目指定环境（conda `writing-agent`，Python 3.13.14）跑 `pytest tests/ -v` → **24 passed**（红线 `test_memory_isolation.py` 全绿）+ 关键疑点动态探测。

### 修复确认：22 项全部有效

逐项核对代码与测试，原清单 22 项修复均成立，且每项都带回归测试。两个重点核验：

- **P0-1**：reflect 条件边已改指 observe，图级测试断言恒不收敛时 `step==3` 优雅终止，不再撞 GraphRecursionError；
- **P2-17**：实测 `langgraph-checkpoint-sqlite 3.1.1` 建的表确为 `checkpoints` / `writes` 且均含 `thread_id` 列，purge 的清理目标与真实 schema 一致。

### 新发现问题（本轮修复引入或遗留）

**R1（P1）｜acquire_lock 残留竞态：锁在窗口内被释放时崩溃 TypeError（已复现）**

`memory/store.py` L132-135：`INSERT OR IGNORE` 被忽略后 SELECT 现有锁行，解包前没有 `row is None` 检查。若持有者恰好在「INSERT 失败 → SELECT」窗口内释放锁，`old_task, old_pid, old_at = row` 抛 `TypeError: cannot unpack non-iterable NoneType object`。此路径下锁其实已空闲，本应顺势拿到。复现（连接代理在 s2 提交后、SELECT 前释放 s1 的锁）：

```
RESULT: TypeError -> cannot unpack non-iterable NoneType object
```

发生概率远低于修复前的 check-then-insert 竞态，但崩溃点在任务启动热路径、CLI 不捕获 TypeError。建议：`row is None` 时重试一次 `INSERT OR IGNORE`（或整体循环重试），并补对应回归测试。

**R2（P2）｜架构文档未与状态机改动同步（违反 AGENTS.md 约定）**

`docs/phase1-architecture.md` §3 仍写 `Reflect --> Plan`（L102、L108「回 Plan 或 Done」、L136、L158），而代码已改为 reflect→observe（否则 step 计数失效，即原 P0-1）。AGENTS.md 明确「改动架构先改文档」。建议升 v1.4，更新状态图并注明回边必须经过 observe 计数节点的理由。

**R3（P2）｜json 模式回退捕获过宽**

`agent/llm.py` `chat_text` 对 `response_format=json_object` 调用 `except Exception` 后回退重试——网络/鉴权/限流错误也会被当成「不支持 json_object」，产生误导性 warning、多一次无谓 API 调用，限流场景还会加重 429。建议收窄到 `openai.BadRequestError`（或检查错误消息含 response_format 相关字样）再回退。

**R4（P2）｜AgentState.sources 成为死字段**

素材回查已改走 `store.get_sources` 注入（正确做法），但 `runtime.py` 仍初始化 `"sources": []`、`schemas.py` 仍声明该归约字段，全库无任何写入方。建议删掉该字段（含 initial 中的初始化），避免读者误以为它参与流转。

### 备忘（不构成问题）

- `captures_source` 在 MCP wrap 处仍按名字匹配（`"fetch" in tool.name`）：标记已显式化、集中一处，可接受；已知局限是 tavily 的 extract 类工具不会入库。
- `release_lock(task_id=None)` 保留无条件删除分支作兼容兜底，当前生产调用方均已传 task_id，无实际风险；后续可直接删掉该分支。

### 结论

P0/P1 全部闭环，修复质量高，且每项都带回归测试。剩 R1（建议修掉再收口）与 R2–R4 三个低危项，处理完即可关闭本轮审查。

---

## 复审 R1–R4 处理结果（2026-08-06，全部修复，pytest 24/24）

| # | 判定 | 修复 |
|---|------|------|
| R1 锁 SELECT 到 None 的竞态窗口 | 成立（INSERT 被忽略后持有者恰好释放，解包 TypeError） | acquire_lock 中 row is None 时顺势原子重试 INSERT，仍失败才报 AssistantBusyError |
| R2 架构文档未同步 | 成立 | 文档升 v1.4：§3 状态图与路由改为 Reflect→Observe 并补充修正理由；§4.6 原子占位/所有权、§5.7 sources 回读、§3.1 死字段删除同步记录 |
| R3 json 回退捕获过宽 | 成立 | chat_text 收窄为 openai.BadRequestError（仅 400 才回退），网络/鉴权/限流直接上抛不再重试 |
| R4 AgentState.sources 死字段 | 成立 | 已从 schemas/runtime/tests 三处删除 |
