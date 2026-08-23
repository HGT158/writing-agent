# 阶段 9 代码审查报告

> 审查对象：v1.25 现状全量代码（非单一提交区间）。此前审查记录只到 phase8 = v1.23 区间 + v1.24 闭环，v1.20–v1.25 多个版本的存量代码未曾整体复审，本报告同时补上 v1.25 缺复审的缺口。
> 审查日期：2026-08-23
> 审查方式：四路并行独立深审（`memory/` 持久化层、`agent/` 核心循环、`api/`+`scheduler/`+`mcp_client/` 基础设施、`web/src` 前端约 5500 行），逐方法核对 assistant_id 隔离与 SQL 参数化、SSE 协议边界、沙箱与脱敏链路；全部发现经第二遍交叉核实到 file:line。另有一份独立第三方审查结果并入比对（其中一项两路独立发现、相互印证，见 P2-1）。
> 测试基线为本次实跑核验（非引自文档）：Python 228/228、前端 vitest 117/117、vue-tsc 通过，与 AGENTS.md/README 声明一致。
> 环境：Windows 11 / conda `writing-agent` 环境，与 AGENTS.md 声明一致。行号均为 2026-08-23 工作区现状（代码文件无未提交改动；文档文件的未提交修改清单见「文档同步核查」——含本报告定稿修订与 v1.26 口径对齐产生的改动）。

---

## 总体评价

先说结论：**工程质量明显高于同规模个人项目**。六节点 Loop 的终态保证、SSE 断线续传协议（前后端两侧）、hunk 级对账状态机、change set 拆表迁移事务、`_safe_relative_path` 对盘符/UNC/ADS/Windows 保留名的路径防御、DOMPurify 消毒等难点经专项核查均未发现漏洞；跨助手隔离红线除一处唯一索引缺列（P2-10）外**逐方法核对全部干净**，SQL 注入为零；8 轮 phase review 的修复痕迹真实可查，README 声明的测试基线账实相符。

真正的短板集中在四个主题：

1. **超时与取消能力不足**：MCP 启动无超时可挂起整个应用启动（P1-8），LLM 调用依赖 SDK 默认 600s 且无逐节总时长上限（P2-3），工具调用的 30s 上限硬编码（P3-18），且无任务取消端点（P2-18）——均为有界的可靠性短板：工具调用必经 `executor.py:67` 的 `asyncio.wait_for(30s)`，不构成「永久锁死」；
2. **MCP 这一侧门架空内置安全声明**：同名工具静默覆盖内置实现击穿 data/ 沙箱承诺，默认幂等声明带来重试副作用；
3. **前端「服务端快照覆盖本地」这一条回写通道**在批量入口、in-flight 击键、undo 语义三个边界上没有设防——对写作工具这是数据丢失形态；
4. **memory 层「先动磁盘后提交库」的补偿只有单层回滚**，与启动对账、跨进程窗口组合出幽灵项目/孤儿意图等错位（P1-2/P2-17）；共享连接的事务纪律在简单 DML 路径裸奔，单次提交异常放大为显式事务写路径的连锁失败（P1-3）。

本阶段发现 **1 个 P0、8 个 P1、18 个 P2、约 35 个 P3/观察项**（定稿前修订：原 P0-2 降级为 P1-3、原 P0-3 撤销并拆解为 P2-18、原 P1-3 降级为 P2-17，见文末「定稿前修订记录」）。与 backlog 已知项重合的已在对应条目标注（SSE 队列上界 = phase7 P3-8、死代码 `_row_to_change_set` = phase7 P3-2、非 JSON 文本不做值级扫描 = phase8 P3-3 既定取舍）；**其余均为新发现**。

---

## P0 — 阻断级

### P0-1 「全部接受」批量入口绕过脏文档确认，单击静默丢弃未保存正文

**位置**：`web/src/App.vue:424-454`（`applyAllChanges`），对照单卡守卫 `applyAgentHunk`（`:329`）、`applyAgentChangeSet`（`:373`）

单卡接受的两条路径都会在目标标签 `dirty` 时弹 `window.confirm`，唯独侧栏「全部接受」在循环里直接 `acceptAllChangeHunks` 后执行 `replaceTab({ ...tab, ..., dirty: false })`（`:440`），无任何 dirty 检查。该按钮作用域是项目级的 `agentProjectChanges`，受影响的不只是当前标签页。触发场景：在文档 A 打了数百字未保存，切到项目下另一文档，在 Agent 面板点「全部接受」→ 文档 A 对应标签被服务端快照整体覆盖且 `dirty:false`，未保存文字无提示消失。写作工具里最痛的数据丢失形态，且单击即可触发、无需任何特殊配置。

**修复建议**：进入循环前对所有受影响 dirty 文档做一次性确认（列出文档清单）；更稳妥的做法是 replaceTab 前若本地内容指纹 ≠ 发送时基线则拒绝对该文档回写。

---

## P1 — 应修复

### P1-1 MCP 同名工具无条件覆盖内置工具，data/ 沙箱约束被静默绕过

**位置**：根因 `agent/executor.py:31-32`（`ToolRegistry.register()` 对同名直接覆盖，无冲突检测）；时序 `agent/runtime.py:44`（先注册内置）→ `runtime.py:58`（后注册 MCP）；附带 `runtime.py:59` 计数逻辑在覆盖发生时算出负数

内置工具之后注册的任何同名 MCP 工具都会静默替换内置版本：`read_file`/`save_markdown` 的 `_safe_resolve` 沙箱校验（`tools.py:26-44`）、`finalize_article` 的非幂等声明随覆盖一并失效，而 Planner 提示词仍按「受 data/ 约束」描述这些工具，直接击穿硬规则第 5 条。触发场景：用户加装最常见的官方 filesystem 类 MCP server——恰好暴露名为 `read_file` 的工具即命中碰撞，此后 Planner 调 `read_file` 走的是无沙箱限制的 MCP 实现，可读全盘并把内容带回上下文/文章。当前仓库配置（tavily/fetch）未命中，属潜伏缺陷。

**修复建议**：`register()` 对已存在名字拒绝覆盖（内置优先）并发 warning；或在注册 MCP 前做名字冲突检查，冲突者跳过并告警。同时修正计数逻辑。

### P1-2 启动对账会删除「正在创建中」的项目目录（不可逆数据丢失）

**位置**：删除点 `memory/projects.py:281`（`recover_project_artifacts`，`__init__` 时对匹配 `_ID_RE` 但不在 projects 表的目录 `shutil.rmtree`）；竞态对手方 `create_project`（`projects.py:770-786`，先建目录写文件后 INSERT+commit）、`_import_project`（`projects.py:1116-1131`，`os.replace(staging)` 与 commit 之间存在裸目录窗口）

另一进程恰在此毫秒级窗口构造 MemoryStore（如 `--purge` CLI、误启第二实例）会把新项目目录删掉，创建方随后 commit 成功 → 库中留下指向不存在目录的幽灵项目，此后对账因「记录存在」永不修复。附带：并发 purge 时对账会把 `.purge-` staging 又 rename 回源位置，purge 事务随后 commit 成功但 `rmtree(staging)` 落空报错，出现「已提交删除但目录被还原」的错位（下次启动兜底，但调用方收到假失败）。

**修复建议**：创建流程改为先 INSERT 后落盘；或向受管目录写 `.meta` 标记文件、对账只删「有标记且校验通过」的目录；对账跳过 mtime 距今小于数分钟的目录；purge 收尾 `rmtree(staging, ignore_errors=True)`。

### P1-3 共享 SQLite 连接的隐式事务泄漏：任一简单写路径提交阶段异常，显式事务写路径连锁失败直至重启（修订：自 P0 降级）

**位置**：无 try/rollback 保护的简单 DML 路径（均为潜在泄漏源）——`memory/project_chat.py:486-521`（add_work_event）、`project_chat.py:334-353`（save_summary）、`memory/short_term.py:177-191、219-224、276-281`、`memory/projects.py:810-815`（rename_project）、`memory/store.py:577-588、627-641、645-654、683-687`（运行锁读写——泄漏源之一，但自身不受害，见下）；受害点为所有 `BEGIN IMMEDIATE`（`projects.py:341、538、585、671、1155、1349、1444、1649`；`project_chat.py:247、367、423、572`）

连接为默认 isolation_level（隐式事务模式）且全进程共享一个 `sqlite3.Connection`。上述「execute → commit」之间若抛异常（磁盘满、busy_timeout 耗尽、Ctrl-C），连接滞留在未决事务且无人 rollback；此后任何显式 `BEGIN IMMEDIATE` 都抛 `cannot start a transaction within a transaction`——保存文档、接受建议、项目聊天落库、写意图登记等显式事务写路径持续失败直至进程重启。**波及面修订（定稿前复核）**：运行锁的 acquire/release 走「INSERT/DELETE + commit」、不经显式 BEGIN，在未决事务中仍可执行（甚至会顺带把烂事务提交掉），初稿所称「加运行锁也失败」不成立；瘫痪面限定为全部显式 BEGIN 路径。讽刺的是所有显式 BEGIN 路径都有 try/rollback，恰恰是简单路径没有。

**修复建议**：连接改 `isolation_level=None`（纯 autocommit）+ 全显式事务；或统一装饰器保证异常路径 rollback；或每次 `BEGIN IMMEDIATE` 前 `if conn.in_transaction: conn.rollback()`。一处改动消除全局性风险。

### P1-4 work_log 值级脱敏系统性缺口：dict 载荷的字符串值完全绕过 `_redact_secrets_in_text`

**位置**：`agent/work_log.py:60-70`（`redact` 只按键名匹配）、`:83-92`（`_redact_string`）、`:95-106`（summarize_args）、`:109-120`（summarize_result）；对照组 `:73-80`（summarize_detail 有值级扫描）

v1.24 修复后值级敏感串扫描只覆盖失败 detail 一条路。args/result 两条主路径上，dict/list 载荷仅当*键名*含 `token/api_key/...` 才打码，非敏感键下的*字符串值内容*不做任何值级扫描——模型把用户粘贴的真实凭据原样回传进 `propose_project_edits` 的 `new_text`/`old_text` 等自由文本字段时，`sk-xxxx` 明文完整保留，写入 `project_chat_work_events.args_summary` 并经 `work_item_start` SSE 实时广播（`runtime.py:408-413`）。这正是 v1.23/v1.24 连续修复的泄漏类别的新绕过面。注：「无法解析的非 JSON 纯文本保持原文」为 phase8 P3-3 既定取舍（见 backlog 条件触发项），本条针对的是 JSON 载荷内部的值缺口，属新发现。

**修复建议**：(a) `redact()` 对 str 叶子追加 `_redact_secrets_in_text(value)`；(b) 补一条「值内嵌凭据不得出现在 args_summary/result_summary」的 RED 用例。

### P1-5 前端保存/AI 应用 in-flight 期间的并发键入被服务端快照无声回滚

**位置**：`web/src/App.vue:287-291`（saveActive）、`:338-340`（applyAgentHunk）、`:381-383`、`:438-441`；配合 `web/src/components/DocumentEditor.vue:310-334` 的最小差异同步

所有回写共用同一模式：`await` 请求 → `replaceTab(server 快照, dirty:false)` → 编辑器 watch 发现 doc 不一致后用最小差异区间把编辑器「纠正」回服务端内容。dirty 确认只发生在点击那一刻；请求 in-flight 的几百毫秒到数秒里用户的每一次击键仍会写入编辑器，随后被这次 dispatch 精确抹掉（`syncingExternalContent` 抑制了 update 事件，连 dirty 标记都不会留下，丢失完全无感知）。点「保存」后继续打字是很常见的肌肉记忆。

**修复建议**：回写前比较 version 是否仍等于发起时的值且本地内容指纹未变；变了则不整体覆盖，改为冲突提示或以本地内容为基线重新定位 hunk。

### P1-6 SSE/fetch 均无停滞看门狗：黑洞网络下面板永久假死

**位置**：`web/src/api/client.ts:154-228`（`watchTask` 重连仅由 `onerror` 驱动）；各 `request()` 调用（`client.ts:12-32`）无 AbortController/超时

TCP 黑洞（休眠恢复、VPN 掉线、NAT 超时）时 EventSource 不触发 error，连接保持「开着」但没有事件到来——客户端没有心跳超时看门狗，UI 永远停在「运行中」、composer 被禁用，只能刷新页面。挂起的 fetch 同样让 busy 永久锁死发送框（`AgentPanel.vue:441` 守卫）。

**修复建议**：`onopen` 后启动空闲计时器（如 60s 无任何事件含服务端注释心跳则主动断开并走既有退避重连）；fetch 加 `AbortSignal.timeout`。

### P1-7 服务无鉴权且无 Host 校验：DNS rebinding 可获得对本 API 的完整读写权

**位置**：`api/main.py:88`（create_app 未装任何中间件）、`api/main.py:516-518`（同源托管静态产物）

未配 CORS 使普通跨域读被浏览器挡住，DELETE/PUT/PATCH 因预检被拦——经典 CSRF 大部分天然免疫。但 DNS rebinding 完全绕开同源策略：恶意站点把域名 A 记录切到 127.0.0.1 后，攻击者 JS 即可读取全部文章、调用 `DELETE /api/projects/{id}?purge=true`（`api/main.py:172-185`）销毁数据、或反复发起烧 LLM 配额的任务。另剩两个真实跨站面：`import-file`/`import-folder`（`api/main.py:187-224`）收 multipart/form-data 属「简单请求」，可被跨站自动表单刷垃圾项目。

**修复建议**：加 Host 头白名单中间件（仅放行 `127.0.0.1:8000`/`localhost:8000`），十几行同时解决 rebinding 与跨站表单；可选再加启动时生成的本地 token。

### P1-8 MCP 启动全程无超时：单个 server 卡住即挂起整个应用启动；半途失败泄漏子进程

**位置**：`mcp_client/client.py:41-44`（stdio 进入后 initialize/list_tools 均无超时包裹）；`api/main.py:80-81`（lifespan 里 `await runtime.start()` 同步等待全部 server 连完）

任一配置的 MCP server 启动即卡住时整个 FastAPI lifespan startup 永久挂起，端口根本不开始监听——「失败不阻断启动」的设计承诺实际不成立。且 `_connect` 中 stdio_client 已进入 AsyncExitStack 后若 initialize 抛错，异常被吞掉继续连下一个，已 spawn 的子进程及管道留在栈里直到应用关闭才清理，长期运行下每次重试泄漏一个进程。

**修复建议**：三个调用都用 `asyncio.wait_for(..., timeout=...)` 包裹；`_connect` 失败时用独立 exit stack 并即时 aclose，只把成功 session 并入长命 stack。

---

## P2 — 建议修复

1. **`interrupt_running()` 迭代字典时被修改，双重故障下丢任务终态**（`agent/work_log.py:314-318`）：循环遍历 `self._items.values()` 时，`done()` 的明细落库失败路径会经 `_note_persist_failure` 往同一字典插入 warning 新键 → 下一次 next() 抛 `RuntimeError: dictionary changed size during iteration`，`kind="task"` 终态行不再写入（被 runtime 补偿 except 吞掉）。触发需「任务失败/取消 且 落库恰好也失败」，概率低但真实（AGENTS.md 自认 Windows WAL 句柄竞态会偶发 PermissionError）。修复一行：迭代 `list(self._items.values())`。**注：本次两路独立审查各自发现此缺陷，相互印证。**
2. **rename/delete document 借用运行锁串行，行为与架构 §5.9「助手级 mutation lock」表述不符**（`memory/store.py:380-392`）：实现直接 acquire_lock 占用 run_locks 表，语义不是「与其他文档写操作串行」而是「与该助手的一切任务互斥」——Agent 正在流式编辑项目时，重命名/删除任何一个（哪怕不相关的）文件都会 409。保守安全没错，但属未登记的产品取舍，用户只会看到莫名的 busy 错误。二选一：架构补记该行为并升版；或改为独立 mutation 串行机制。**（已由 v1.26 按文档分支关闭：§5.9 明确该锁以助手运行锁实现、任务运行期间拒绝 409；若后续要求「仅与并发文档写互斥」，再走代码分支并升版。）**
3. **LLM 调用全程无显式超时**（`agent/llm.py:121-140、63-77`；消费点 `agent/loop.py:151-164`、`runtime.py:220-243`）：全部依赖 AsyncOpenAI SDK 默认（read 600s）。Planner 每轮 + 每节写作 + 质检，最坏一个 25 步任务可在单节点上挂 10 分钟级，期间 SSE 仅靠 keepalive 维持、无终态。对第三方 base_url 网关尤其脆弱。建议构造 client 时显式传 timeout，并为逐节流式加总时长上限。
4. **SQLite/文件重 IO 直接跑在 uvicorn 单事件循环上**（典型 `api/main.py:188-224` 导入接口；底层 `memory/projects.py:1094-1136` 最高 512MB 逐文件读写、archive/purge 的 shutil 操作；`store.py:463-478` 导入全程持 MemoryStore 全局锁）：大导入期间**所有**请求与其他任务的 SSE token 流一起停摆，15s 心跳一旦被拖过，前端会误判断线触发重连风暴。建议重操作用 `anyio.to_thread` 包装（MemoryStore 已有 threading.Lock + WAL，线程化安全）或改为后台任务。
5. **MCP 工具默认按幂等处理并被超时重试，写类工具有重复副作用风险**（协议默认 `agent/schemas.py:57-59` idempotent=True；执行 `agent/executor.py:63-65` 幂等即 attempts=2；源头 `mcp_client/client.py:58-66` 从不声明）：`asyncio.wait_for` 超时取消的只是客户端等待，服务端副作用可能已完成，随后自动重试一次。`captures_source` 也仅靠 `"fetch" in tool.name` 子串猜测。建议 MCP 包装保守地默认 `idempotent=False`、`captures_source` 无法可靠推断时一律 False。
6. **SSE 订阅者队列无上界，慢消费者内存积压**（`api/tasks.py:125` 无 maxsize、`:59` put_nowait 永不背压；`:54-57` 的 4096 窗口裁剪不释放队列仍持有的引用）：页面转后台/冻结时生成器停在 yield 上，生产侧照常入队，长任务可把全部 token 流驻留内存，多订阅者线性放大。**backlog 已登记为条件项（phase7 P3-8：开放远程访问前处理）**，维持暂缓合理，建议届时一并加有界队列 + 僵尸订阅者剔除。
7. **任务提交预检 TOCTOU，并发提交产生「假 202」**（预检 `api/main.py:92-95` 仅查 is_locked；真正加锁在后台协程 `agent/runtime.py:87,151,259`）：连续两次 POST 都能通过预检拿 202，随后其一后台变 `task_failed`。无数据损坏（INSERT OR IGNORE 原子兜底），但 API 语义是「受理后又失败」。建议请求路径上同步占位锁，冲突当场 409。
8. **RUN_LOCK_TTL 只对加锁侧生效，存活判定硬编码 2 小时**（`memory/store.py:674` 写死 `timedelta(hours=2)`，对照 `store.py:574` 与 `config/settings.py:57` 的可配 ttl_hours）：调大调小都不影响 is_locked/current_lock_task_id/mutation 拒绝三条路径，两条判定行为割裂，排查极具迷惑性。TTL 应下沉为 MemoryStore 实例字段两处共用。
9. **同进程写意图恢复可抢占活跃写者**（`memory/projects.py:506` 对 `owner_pid == os.getpid()` 直接判死；恢复入口 get_document `:1044-1053`、reject_change_hunk `:1647`、create_change_set_hunks `:1347`、create_selection_change_set `:1441` 均绕过 per-doc guard）：当前单事件循环线程难触发，但一旦引入线程池/to_thread 化，读者会在写者「已提交→未 finalize」窗口内抢认领并整文件重写，写者 finalize 时三元组不匹配收到假 409（内容其实已写）。建议同进程 PID 改 claimed_at 宽限期判断，或所有恢复入口先取 guard。
10. **change set 唯一约束缺 assistant_id，隔离红线偏离**（查询 `memory/projects.py:1299-1306` 仅按 (task_id, document_id)；索引 `projects.py:328-331,376-379` UNIQUE(task_id, document_id)）：task_id 为各助手独立 uuid 时只是理论碰撞，但违反「所有查询以 assistant_id 隔离」红线第 3 条的字面要求。建议查询补 assistant_id、索引重建为 UNIQUE(assistant_id, task_id, document_id)。
11. **long_term/purge 路径不校验 assistant_id，防御纵深缺口**（`memory/long_term.py:14-22、28-43` profile_path 直接拼接；`store.py:701` rmtree articles/<aid>）：id 含 `..` 或盘符可直接逃逸 data_dir。当前 API 层经 registry 校验存在性兜住了，但 memory 层自身对这条红线不设防。建议 `_validate_id` 下沉为公共函数复用。
12. **rename_document 的 TOCTOU 与二次失败补偿缺口**（`memory/projects.py:866-881`）：`exists()` 检查与 `os.rename` 之间有窗口（POSIX 下静默覆盖同名文件）；回滚是单层的——UPDATE 失败后的反向 rename 若也失败（Windows 文件占用很常见），盘上新路径、库里旧路径，无对账机制能修复。delete_document 补偿（`:928-933`）同样单层。建议两阶段写入 + 回滚失败落待对账标记纳入 recover。
13. **外部内容替换进入撤销栈，Ctrl+Z 造成与服务端版本背离**（`web/src/components/DocumentEditor.vue:310-334`）：最小差异同步用普通 dispatch，basicSetup history 将其记入 undo 栈；保存/接受 hunk 后 Ctrl+Z 回退正文且 tab 变 dirty，本地落后于已递增的服务端版本，下次保存 409。建议 dispatch 附 `isolateHistory.of("full")` 注记。
14. **reconcileChanges 分页终止条件写错，老项目每次打开全量翻页**（`web/src/App.vue:214-237`）：`collected.length >= result.total` 中 collected 只累加过滤后条目而 total 是该文档全部 change set 数，条件几乎恒假，只能靠空页兜底跳出——每次打开文档/保存后对账都拉几十页历史。不会死循环，但延迟随历史线性增长。改用拉取数与 total 比较。
15. **token 预算兜底在 system prompt 本身过大时失效**（`agent/context.py:123-135` allowance 被 floor 到下限、`:100` 把 token 预算当字符数用）：调小 CHAT_CONTEXT_TOKEN_BUDGET 而保持 DOC_MAX_CHARS 时最终 prompt 仍显著超预算，且只有局部截断 warning 没有「总量仍超预算」警告，打破 v1.21「prompt 恒不超预算」承诺。建议 system 超支时显式 warning 并按 token 而非字符裁剪注入正文。
16. **Reflect 质检 JSON 解析失败默认判「通过」**（`agent/loop.py:205-206` `except json.JSONDecodeError: passed=True`）：质检模型连续输出坏 JSON → 弱草稿未经任何核查直接定稿。建议默认 passed=False 计入 reflect_fails，让连败保护接管。
17. **archive/purge 不检查 document_write_intents，与在途写者交叉（修订：自 P1 降级）**（`memory/projects.py:937-957` archive_project 仅检查 pending change sets；`projects.py:973-1026` purge 在 `:1000-1003` 直接删意图行；对照 `:821-838` `_reject_document_mutation`）：与另一进程「意图已提交、文件未写完」窗口交叉时，在途写者得到的是干净报错（临时文件写入/os.replace 在目录被 move 后失败，意图被 discard）；归档项目此后所有端点 404、无人再触发恢复路径，初稿所称「该文档永久卡死」不可达。实际残留是孤儿意图行（archive 不清理、purge 才清理）与防御纵深缺失；save_document 门面不持运行锁（`store.py:492-500`），运行锁挡不住这个交叉。建议 archive/purge 前按 `_reject_document_mutation` 语义拒绝存在活跃意图的项目。
18. **无任务取消端点（修订：原 P0-3 的有效残余，降级为增强项）**（`api/main.py` 只有 `GET /api/tasks/{task_id}`）：原 P0-3 以「MCP 调用无超时 → 任务永久挂起 → 锁永不释放」定级，前提不成立——`agent/executor.py:67` 对全部工具调用施加 `asyncio.wait_for(30s)`（幂等工具最多 60s，即本报告 P3-18），LLM 调用有 OpenAI SDK 默认 600s 兜底，任务必达终态、锁由 finally 释放；「过期但 pid 存活不回收」是架构 §4.6 的既定设计而非缺陷。剩余价值是运维便利：增加 `POST /api/tasks/{task_id}/cancel`（runner 已有 CancelledError 分支，成本低）；`_live_lock_locked` 的 TTL 硬编码问题归 P2-8 一并处理。

---

## P3 — 可优化 / 观察项

### memory 层

1. 死代码 `_row_to_change_set`（`projects.py:404-410`）按旧 schema 取列，一旦被调用即 TypeError——**即 backlog 已登记的 phase7 P3-2**，建议尽快删除。
2. document_write_intents 的 CREATE TABLE 缺 `hunk_id` 列（`projects.py:86-102`），全靠 create_tables 的 ALTER 兜底（`:309-320`），新库结构正确纯靠迁移路径巧合；应补进 PROJECT_DDL。
3. work_events 两处 `INSERT OR IGNORE` 静默吞并：kind='task' 被 (task_id,event_seq) 唯一索引误吞时 SELECT 返回 None → TypeError（`project_chat.py:486-502`）；interrupt_work_task 的 MAX+1 撞 seq 时静默不写终态，任务永远显示未完成（`:586-602`）。IGNORE 后应判空重试或转 ResourceConflictError。
4. `fromisoformat` 未捕获（`store.py:614、674`）：残留锁行 acquired_at 格式异常时抛 ValueError，该助手所有加锁操作不可用。建议视为过期回收。
5. `_write_atomic` 无 fsync（`projects.py:473-480`）：断电可能丢最近一次保存（WAL 只保护库不保护文档文件）。
6. 大文件流程重复全文读取：一次保存全文读 2-3 遍（`projects.py:469、1156、1361`），delete_document 全量读 payload（`:911-914`）；建议合并 BOM 探测与内容加载。
7. purge_assistant 清理不彻底（`store.py:693-718`）：archive/projects/<aid>/ 与 profile.md 永不清理（对账只处理 .purge- 前缀）；且 `project_chat.delete_assistant_rows`（`project_chat.py:609-621`）不 commit，单独复用会丢删。建议直接 rmtree 整棵助手目录。
8. 导入重名用精确字符串比较（`projects.py:1098-1100`）：Windows 大小写不敏感 FS 上 "A.md"/"a.md" 都过查重，第二个覆盖第一个文件而库里留两行。建议 casefold+NFC 归一键。
9. change set 唯一索引兜底时并发双提交后者抛 IntegrityError 裸 500（`projects.py:1372,1396`）；应捕获转 ResourceConflictError。
10. delete_document 只挡 pending change sets，已 applied/rejected 的记录留在库里指向已删文档（`projects.py:885-934`），可查出幽灵记录。建议随删。

### agent 层

11. `finalize_article` 半完成态：文件已落盘但 memorize 失败 → Planner 下轮重新规划 → 重复定稿文件与重复记忆条目（`agent/tools.py:67-68`）。建议 memorize 失败降级 warning 走成功路径。
12. 同步阻塞 IO 直接跑在事件循环里（`tools.py:78,85`、`loop.py:246`、assistant_registry 多处）：网络盘/杀毒扫描时阻塞全部并发任务。建议 `asyncio.to_thread`。
13. `_note_persist_failure` 的第二次失败完全静默（`work_log.py:303-306` `except Exception: pass`），违背「禁止吞异常」约定，至少补 logger.debug(exc_info=True)。
14. 值级敏感串模式覆盖面有限（`work_log.py:31-39`）：短于 8 字符不匹配、缺 ghp_/AKIA/AIza/xoxb 等常见前缀。按需逐步扩充。
15. API 进程启动期的助手解析警告永久丢失（`runtime.py:40-41` 在 broker 订阅者建立前 emit；CLI 先 subscribe 幸免）。建议同时 logger.warning。
16. 失败信号双重发射（bus.emit("failed") 又上抛由 broker 补终态，`runtime.py:214-216,544-545`）；且 chat_project 失败时 user 消息已落库而 assistant 侧无占位，重试后会话出现连续两条用户消息。建议失败统一交 broker 终态表达 + 失败轮次落 interrupted 占位。
17. `--resume` 的 operator.add 通道残留旧观察/草稿（`runtime.py:103-115`、`schemas.py:70-73`）：误 resume 到旧 session 时旧观察混入新任务甚至被定稿。建议 resume 入口校验 thread 归属。
18. 流式细节：不检查 finish_reason，length 截断的半截文本静默入库为正式回复（`llm.py:54-118`）；parallel_tool_calls 被网关拒绝时直接 RuntimeError 终止整个项目编辑能力而非去参重试（`llm.py:74-77`）；executor 30s 超时硬编码且重试无退避（`executor.py:67`），建议提为 Settings 常量。

### api / 基础设施层

19. `stream()` 首帧前的窄竞态：校验与生成器首迭代之间记录可能被 _trim_records 弹出，KeyError 变成连接异常中断而非干净 404（`api/main.py:500-503`、`api/tasks.py:123-124`）。概率极低，可在生成器内捕获 KeyError 正常终止。
20. 文件名校验漏控制字符：`\n`/`\x01` 通过校验后在 Windows os.rename/unlink 抛 OSError → 未映射 500（校验 `projects.py:208-223`；`_raise_http` `api/main.py:34-46` 无 OSError 分支）。校验补 `ord(ch) < 0x20` 即可。
21. GET 端点带写副作用（`api/main.py:437-447` session 详情接口执行 interrupt 写库），违反 GET 幂等语义，应拆显式 POST。
22. Web 服务模式下 scheduler 永不启用（`agent/runtime.py:52` 默认 False、`api/main.py:81` 未传参），daily job 只在 CLI schedule 模式运行，而 README 主入口是 uvicorn——至少应在启动日志/文档明示。APScheduler 本身实现无误。
23. MCP 子进程继承全量环境变量（`mcp_client/registry.py:79` `{**os.environ, ...}`），OPENAI_API_KEY 注入每一个第三方 server。建议白名单透传。
24. 密钥泄漏面核查未见直接泄漏（.env 仅进环境、错误响应不含 key、openai SDK 自脱敏）；残余面：若有人在 mcp_servers.json args 里内联 `${SECRET}` 展开值，spawn 类异常文本会把命令行带进日志（`client.py:36`）。
25. 请求体无全局字节上限：Pydantic 层有限制（models.py:35 的 2MB、:14 的 100k）但 Starlette 先整体读入内存再校验；multipart 导入限额在解析后才生效。本地面危害有限，建议 uvicorn 层加限制。同类：selected_text/session_id 无长度约束（models.py:49、15）。

### web 前端

26. 切换/新建聊天会话连带清掉其它项目仍有效的待审卡片（`App.vue:60-65` 按 source 一刀切过滤；好在 openDocument 会触发 reconcile 自愈，仅瞬时视觉漂移）。建议按 chat_session_id 过滤。
27. unicode 转换在大文档多 hunk 时每击键 O(h·n)（`utils/unicodeOffsets.ts:6-10`、使用处 `DocumentEditor.vue:56-104`，内部 Array.from 整篇复制码点数组）。建议按 content 引用 memoize。
28. focusHunk 回退搜索不校验唯一性，可能命中同文异处（`DocumentEditor.vue:152-168`，对照 locatedHunks `:74-77` 有唯一性判断）。建议复用唯一性判断。
29. 选区改写在 dirty 文档上必然失配且提示不可懂（`DocumentEditor.vue:234-252`：start/end 基于本地内容计算却附旧版本号）。建议 toolbar 出现时 dirty 则置灰并提示「请先保存」。
30. Markdown 预览链接未做 target/rel 加固（`MarkdownPreview.vue:7-13`；XSS 本身已被 DOMPurify 兜住，此处仅防整页跳离）。建议 afterSanitizeAttributes hook 补 target="_blank" rel="noopener noreferrer"。
31. openDocument 并发完成顺序未保证，活动标签可能回到旧目标（`stores/workspace.ts:39-59`）。建议请求级自增序号。
32. 会话生命周期内的轻度无界累积：workRecords 只增不减（`AgentPanel.vue:266`）；result_summary/detail 未像 delta 一样截断 500 字符（`:236-239`）。
33. 合法的空 change_preview（任务成功但无修改）被当成错误弹「无效的修改预览」（`types.ts:143` 要求 hunks.length > 0；消费方 `AgentPanel.vue:492-496`、`DocumentEditor.vue:264-270`）。空数组应静默处理。

### 文档口径类（需升版或登记，二选一）

34. save_summary 残留 assert 兜底（`memory/project_chat.py:355`）：v1.21 已明确把 runtime 分支 assert 改显式 raise 以消除对 python -O 的隐式依赖，此处是同类遗漏。
35. 任务终态记录只按容量 128 条有界保留、无 TTL，而架构 §5.9 写「按 TTL/容量有界保留」——内存安全，属文档口径二选一。**（已由 v1.26 关闭：§5.9 修正为按容量有界保留。）**
36. 架构 §3.3 说 fetch 全文「截断至 2000 字符进 Observation」，实现是 summary ≤500 字符 + 全文落 sources（`executor.py`）——实现更严格，数字不一致。**（已由 v1.26 关闭：§3.3 修正为全文 ≤20,000 字符落 sources、≤500 字符摘要进 Observation。）**
37. `chat_project` 不检查助手技能子集是否包含 editing（rewrite_selection 检查了），两入口策略不一致，架构未明确项目聊天是否受子集约束。**（已由 v1.26 关闭：§5.4 明确项目聊天始终注入 editing 指导、不受子集裁剪，选区改写仍校验。）**
38. `_accept_hunk_impl`/`_save_document_impl` 在 BEGIN IMMEDIATE 事务内做文件读取/BOM 探测，大文件拉长写锁持有时间（架构禁令针对意图三段式写路径，读路径未禁止，本地单用户可接受，仅记录）。
39. ProjectExplorer 文件重命名输入框允许输入含 `/` 的名字变相移动文件到子目录（服务端合法所以无害，与「只编辑文件名」交互意图略偏）。

---

## 已核实无问题的关键面

以下经专项核查未发现问题，可作为回归信心：

- **隔离红线**：除 P2-10 的唯一索引缺列外，逐方法核对 WHERE 条件，含经 project_id/document_id/session_id 二次查出的入口均带 assistant_id 前置校验；**SQL 注入为零**（动态 SQL 仅限硬编码标识符 + 参数化占位符）。
- **沙箱**：`_safe_resolve` resolve-after-join + parents 校验能挡绝对路径、`..` 与符号链接逃逸；`_reject_managed_assistant_write` 覆盖 assistants/ 全域；`_safe_relative_path` 对盘符/UNC/ADS/保留名/尾随点空格防御严密。
- **Loop 正确性**：回边强制经过 observe 计数节点使 max_steps 真实生效，recursion_limit 双保险，四类路由（含未知动作默认 done、Planner 连败强制 finish）保证终态可达；broker catch-all 终态 + keepalive + 游标续传使事件流不因业务异常悬挂。
- **SSE 协议（前后端）**：游标 clamp 保终态必达、reconnect_gap 处理窗口外游标、「订阅先入队后拍快照」无丢失、seq 更新发生在 gapped 抑制之前故游标不回退、终态事件因窗口尾部追加永不被裁剪；前端退避序列封顶且 onopen 归零、seq ≤ lastSeq 去重幂等、缺口后只放行终态并重载持久化会话，链路闭环。
- **写意图三段式与崩溃恢复**、**hunk 内容复检/stale 语义**、**change_set_hunks 单事务迁移**（BEGIN IMMEDIATE + 任一步失败整体回滚 + 合成 task_id 规避 NULL 唯一索引语义，范本级实现）、FTS trigram 迁移与 LIKE 转义采样。
- **前端**：XSS 唯一 v-html 经 DOMPurify；hunk 状态机客户端不乐观翻转、一律以服务端响应为准，双视图共用单一状态源纯派生；unicode offset 双向转换约定一致且有代理对测试；localStorage/主题降级严谨；API 层 res.ok 统一检查、未发现 unhandled rejection 路径；EventSource 生命周期干净（卸载 stopStream、终态 finish、close 取消定时器）。
- **Planner 容错**（双次尝试 + 错误回喂 + 强类型校验 + 兜底 finish）、**密钥链路**（key 不进日志/错误响应）。
- **文档与代码同步良好的部分**：§5.9 API 表与路由逐条对上（含 v1.25 两个新端点）；工作记录上限/截断/脱敏常量与 work_log.py 精确一致；`.env.example` 与 settings.py 字段一致；docs 四目录合规、导航准确；backlog 登记的暂缓项逐一确认仍在代码中（如 watchTask onopen 即重置退避 = phase7 P3-10）。

---

## 文档同步核查

1. **HEAD 提交里 README.md 仍指向 v1.24（两处），v1.25 提交时漏更新**。工作区那批未提交改动（README 两处 v1.24→v1.25、AGENTS.md 一处措辞、backlog 分类重排、phase8 review 删去过时的处理结果节）逐一核对过，**内容全部正确且正是需要的同步修正**，只是没提交。建议审查后尽快单独提交这批文档，避免下个阶段提交混入无关 diff。
2. **v1.25 没有对应复审报告**（reviews 只到 phase8 = v1.23 区间 + v1.24 闭环），本报告补上该缺口。
3. P2-2（mutation lock 表述）与 P3-35/36/37 三个口径项已由 v1.26 文档口径对齐关闭（仅更新架构文档并升版、同步各处版本引用，无任何代码变更）；P3-34（`save_summary` 残留 assert）属代码修复，仍待加固批次处理。

---

## 验证记录

- 四路并行独立审查，全部发现经第二遍交叉核实定位到 file:line；另并入一份独立第三方审查结果比对：其 P2-1 与本方 agent 组发现完全一致（交叉印证），其 mutation lock 表述偏差（本报告 P2-2）、assert 残留与五个口径/交互观察项（P3-34 至 P3-39）为本报告新增来源。
- 测试基线为本次实跑核验：conda 环境全量 `pytest tests -q --basetemp D:\test_agent\pytest-temp-review-20260823` → **228 passed**（14.69s）；`npm test` → **117 passed**；`npm run typecheck`（vue-tsc -b）通过。与 AGENTS.md/README 声明精确一致。
- 仓库卫生：`.env`/`data/`/`*.db*`/`*.log`/node_modules/pytest-temp-* 均未被 git 追踪且 `.gitignore` 覆盖完整；`config/mcp_servers.json` 无内联密钥（`${TAVILY_API_KEY}`/`${LOCAL_PROXY}` 插值）；requirements 版本约束合理且注释了上限理由（mcp<2 的 McpError 改名等）。未提交改动全部为文档文件（见上节），无代码改动。
- 本报告为唯一新增文件；未改动任何代码。行号以 2026-08-23 工作区现状为准。

## 处理建议

按项目惯例本次不改动任何代码。建议分三个梯队：

1. **第一梯队**（改动极小、收益最大，建议作为一个加固批次尽快落地）：P0-1 applyAllChanges 补 dirty 确认；P1-3 连接 isolation_level=None（一处配置）；P1-1 register 冲突检测（一两行 + 测试）；P1-4 redact str 叶子值级扫描 + RED 用例；P1-7 Host 白名单中间件（十几行）。
2. **第二梯队**：P1-2 对账标记；P2-17 archive/purge 意图检查；P1-5 回写版本/指纹校验；P1-6 空闲看门狗；P1-8 MCP 启动超时与 exit stack 修复；P2-1 迭代 list 化（一行）。
3. **第三梯队**：其余 P2 按模块顺手处理（P2-18 取消端点为增强项）；P3 归入既有「加固批次」排期。文档口径四项（P2-2 与 P3-35/36/37）已由 v1.26 架构口径对齐关闭（纯文档升版，无代码变更）；P3-34（`save_summary` 残留 assert 改显式 raise）属代码修复，并入加固批次。

实施任一项请遵循 RED → GREEN 与全量回归，涉及契约变化的先升版架构文档。

---

## 定稿前修订记录（2026-08-23）

初稿定级 3 个 P0。定稿前经第二轮交叉复核修正三处，正文编号与计数已同步更新：

- **原 P0-3 撤销**：「MCP `call_tool` 无超时、一次挂起永久锁死助手」的前提不成立——`agent/executor.py:67` 对全部工具调用施加 `asyncio.wait_for(30s)`（初稿 P3-18 亦记载该超时，与 P0-3 自相矛盾），LLM 调用有 OpenAI SDK 默认 600s 兜底，任务必达终态、锁由 finally 释放；「过期但 pid 存活不回收」为架构 §4.6 的既定设计而非缺陷。有效残余（无取消端点）降级为 P2-18，TTL 硬编码归 P2-8，MCP 启动挂起归 P1-8。
- **原 P0-2 降级为 P1-3 并修正波及面**：事务泄漏机制属实（简单 DML 路径无 rollback，异常后连接滞留未决事务，后续显式 `BEGIN IMMEDIATE` 全部失败），但「加运行锁也失败」有误——锁路径不经显式 BEGIN，在未决事务中仍可执行；瘫痪面修正为全部显式事务写路径，运行锁路径重新标注为泄漏源而非受害点。
- **原 P1-3（archive/purge 意图检查）降级为 P2-17**：归档后项目端点全部 404，「文档永久卡死」不可达；实际影响修正为在途写者干净报错 + 孤儿意图行残留 + 防御纵深缺失。
