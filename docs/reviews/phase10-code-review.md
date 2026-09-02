# 阶段 10 代码审查报告

> 审查对象：v1.28–v1.31 四个已提交区间（c529c26 → 01741fc，共约 +3,970/-220 行），即 phase9 复审（v1.25）与 v1.27 两梯队处理闭环之后的新增改动：v1.28 助手系统提示词可写可编辑、v1.29 加固批次（写意图契约/终态守卫/SSE 退避）、v1.30 助手记忆系统完善、v1.31 TRAE 式模型/提供商切换与温度配置化。
> 审查日期：2026-09-02
> 审查方式：四路并行按提交独立深审（每路全量 diff 逐行精读并对照 HEAD 工作区复核），跨提交同区域改动单独交叉核对；全部 P1/P2 关键发现另经主审逐条亲验到 file:line。其中「编辑对话框对空描述助手必然 422」一项由 v1.28 与 v1.30 两路独立发现、相互印证（见 P1-1）。
> 测试基线为本次实跑核验（非引自文档）：Python 344/344（19.9s）、前端 vitest 174/174（15 文件）、vue-tsc 通过，与 AGENTS.md 声明一致。
> 环境：Windows 11 / conda `writing-agent` 环境，与 AGENTS.md 声明一致。行号均为 2026-09-02 工作区现状（HEAD 01741fc，工作区无未提交改动）。

---

## 总体评价

先说结论：**工程纪律延续 phase9 水准，无 P0；但两个新系统（助手记忆、模型提供商配置）都存在「写入收口」与「读出约束」的一次性缺口，其中两条组合出静默的上下文/数据丢失形态。**

经逐方法核对仍然干净的面：跨助手隔离红线在四个提交的全部新端点与新查询上成立（FTS 检索 `WHERE assistant_id = ?`、recall/profile/change set 均有跨助手隔离或 404 的实质测试）；新代码零 SQL 出 `memory/`；密钥边界符合硬性规则第 4 条（GET 只回掩码尾缀、前端 password 输入不落 localStorage、`llm_providers.json` 已 gitignore 且未被跟踪、`.env.example` 仅占位）；温度硬编码收敛彻底（全仓无残留节点级硬编码）；按任务快照路由三路径（run/chat_project/rewrite_selection）一致性正确，v1.31 还顺手修复了 v1.30 沉淀调用用 `self.llm` 的运行中切换漂移隐患；v1.29 五项加固真实落地且契约变更的所有调用方同步。四个版本的 changelog 与实现总体相符。

真正的短板集中在三个主题：

1. **助手记忆系统（v1.30）的写读两端都没有上界**：写侧显式指令直达路径无长度上限、无去重、无裁决（P1-2），读侧 50,000 字符的画像上限与 24,000 token 的聊天预算不匹配且注入块自身永不裁剪——大画像会静默把文档上下文清零、历史窗口清空（P1-3）。两条叠加意味着「记住：<长文>」一句话就能持续劣化之后所有轮次，且无任何指因提示。
2. **v1.30 对 v1.28 契约的单边收紧制造了一条跨版本交叉回归**：`AssistantUpdate.description` 加 `min_length=1` 而前端编辑对话框恒携带该字段，空描述助手在 UI 中不可编辑（P1-1，两路独立发现）。这是「Pydantic 校验收紧需两端联动」的典型样本，且现网测试对空描述 PATCH 无覆盖，缺口因此存活。
3. **持久化文件写入标准的落差**：v1.31 为 `llm_providers.json` 引入「临时文件+原子替换」，但 v1.28 的 `assistant.yaml`/`persona.md` 仍是原地截断覆写（P2-3），v1.30 的 `profile.md` 同样（P3-10）；v1.31 自身的并发切换又有撕裂读竞态，可把损坏的 (provider, model) 配对持久化，下次启动 RuntimeError、按提示删文件重建则丢失全部手工提供商（P2-1）。

本阶段发现 **3 个 P1、8 个 P2、23 个 P3/观察项，无 P0**。与 backlog 已知项重合的已在对应条目标注（探活按钮缺失 = v1.31 已登记暂缓；非 JSON 纯文本不做值级扫描 = phase8 P3-3 既定取舍）；**其余均为新发现**。

---

## P0 — 阻断级

本轮未发现 P0 级问题。phase9 的 P0（「全部接受」绕过脏文档确认）已在 v1.27 修复并经本次回归面核对未复发；本区间四个提交均未触碰该路径。

---

## P1 — 应修复

### P1-1 「描述为空」的助手在 UI 中必然无法保存任何编辑（422），提示为不可读的状态码文本（v1.30 引入，经 v1.28 前端显现；两路独立审查相互印证）

**位置**：根因 `api/models.py:17`——`AssistantUpdate.description` 加了 `min_length=1`（diff 可证为 94b0cb9 单边收紧，v1.28 时点仅 `max_length=500`）；触发点 `web/src/components/AssistantDialog.vue:45`（提交载荷恒携带 `description: description.value.trim()`）与 `web/src/App.vue:186-188`（`updateAssistant` 恒把 description 放进 PATCH body，不做「未变更则省略」）；放大器 `web/src/api/client.ts:20-31`——FastAPI 422 的 `detail` 是数组，既非 string 也非 `{message}` 对象，回退显示 `422 Unprocessable Entity` 状态文本。

创建入口允许空描述（`AssistantCreate.description` 无 `min_length`），因此「描述为空」是常见状态。这类助手在标题栏打开编辑对话框后，不改描述直接点「保存」（哪怕只想改 persona）就发出 `description: ""` → 422，对话框报一串不可读的状态码并保留。用户除非随手在描述里敲入任意非空文字，否则无法保存任何修改。同一字段「创建允许空、更新拒绝空」自相矛盾；与架构 §5.10「服务端拒绝原样提示」的体验承诺相悖。94b0cb9 未同步改前端、未补测试（tests 无 `min_length` 断言），可判定为非预期交互回归。

**修复建议**：二者取一——(a) 回退 `min_length=1`（空串落空描述，与创建语义对齐，推荐）；(b) 前端仅在实际变更时携带 description 字段。无论哪种，补一条「编辑空描述助手可保存」回归测试，并让 `client.ts` 对 422 数组 detail 取首条 `msg` 拼接。

### P1-2 显式指令直达沉淀无长度上限、无去重、无裁决：单条消息可把画像永久污染（v1.30）

**位置**：根因 `agent/chat_memory.py:15-18`（`_EXPLICIT_COMMAND` 把指令词之后的全部剩余内容捕获，`re.DOTALL` 含换行）与 `:40-46`（原样返回）；直达调用点 `agent/runtime.py:389-395`——`direct` 未经 `MAX_ITEM_CHARS`（120 字，仅启发式路径使用）截断、未经与画像比对，直接 `store.memorize(assistant_id, "preference", direct, ...)`；放大器 `memory/long_term.py:37-52`（`append_profile` 无单条字符上限，仅 200 行裁剪）；入口面 `api/models.py:95`（聊天消息上限 100,000 字符）。

触发场景：用户发送「记住：<粘贴 8 万字文档>」，该内容整段写入 `profile.md`，此后**每一轮项目聊天**的 system prompt 都注入全文（`runtime.py:476-489`），**每个普通任务**同样（Planner prompt）；同一句话重复说一遍就多一条重复条目（直达路径完全无去重）。正则还有误判类：疑问句「你记住我要写什么了吗」会把「我要写什么了吗」当偏好直存。该路径零模型调用意味着没有任何裁决点，写入即永久，只能靠用户自己发现并手工打开「记忆画像」对话框清理。与 P1-3 叠加后，一条消息即可持续劣化所有后续轮次。

**修复建议**：直达路径复用 `MAX_ITEM_CHARS` 截断（多行内容拒绝直达、降级启发式提取更稳）；落库前与画像既有行做规范化比对去重；正则要求指令词后紧跟分隔符，减少疑问句误判。

### P1-3 画像上限 50,000 字符与聊天 token 预算 24,000 不匹配，注入记忆块自身永不裁剪：大画像静默清空文档上下文与历史窗口（v1.30）

**位置**：根因 `memory/long_term.py:19`（`ASSISTANT_PROFILE_MAX_CHARS = 50_000`，恰为本提交 PUT 端点放行的上限）与 `config/settings.py:41`（`chat_context_token_budget = 24000`）；注入块 `memory_trace.text` 在 `agent/runtime.py:476-489` 直接拼入 system prompt，无任何针对记忆自身的裁剪；后果链 `runtime.py:493-506`（`max_document_tokens = max(budget − estimate(fixed_system) − 5, 0)`，记忆全文计入 `fixed_system`）→ `agent/context.py:60-61`（`max_tokens <= 0` 返回 `("", True)`）；历史全丢点 `context.py:202-208`（`system_tokens >= budget` 返回空窗口，仅一条不指因的 warning）。

token 估算对 CJK 约 1 字 1 token（`context.py:25-36`）。用户经「记忆画像」对话框保存约 24k 汉字画像（完全合法，PUT 校验放行）后，每次聊天文档预算被算成 0：模型看到「content（已按上下文预算截断）：」后跟**空正文**——说「帮我改第二段」，`propose_project_edits` 因看不到正文产出错误 hunk 或答非所问；历史对话同时全部被弃。截断标签反而暗示「文档太长」，全程没有任何指因于记忆过大的提示。普通任务路径同理（`memory_context` 无裁剪进 Planner prompt）。提取调用还把画像全文塞进提取 prompt（`chat_memory.py:69`），放大成本。

**修复建议**：注入前对记忆块按预算占比裁剪（如 `min(估算, budget/3)`，复用 `clip_content_to_token_budget`）；或在 Memory 层区分「参与注入的画像」与「白盒存储全文」两个上限；至少在文档被挤到 0 时发一条指明原因的 warning。同时考虑把「画像参与注入的有效上限」降到与预算匹配的量级（如 8,000 字符）。

---

## P2 — 建议修复

1. **（v1.31）`_flush`/`payload` 对 `_selection` 撕裂读：并发切换可持久化交叉配对，下次启动 RuntimeError，删文件重建丢失全部手工提供商**（根因 `agent/llm_providers.py:259-261`——`provider_id` 与 `model` 是对 `self._selection` 的两次独立属性读；`payload()` `:198-201` 同型；触发点 `api/main.py:227-243`——`add/select` 经 `asyncio.to_thread` 在工作线程执行，而前端 `AgentPanel.vue:109-116` 的 `selectProvider` 不置 busy，快速连续点选两个模型即产生并发 in-flight POST）。线程 A 的 `_flush` 在两次读之间被切走、B 完成赋值，落盘 `current` 变成 `(p1, m2)` 混合配对——跨提供商切换时 m2 必不在 p1 的 models 里，下次任何进程构造 Runtime 时 `_load`（`llm_providers.py:171-175`）直接 RuntimeError，Web 服务与 CLI 全部无法启动；按报错提示删文件重建则**丢失全部手工添加的提供商**。「显式报错不静默回退」的设计反而放大了这次竞态的后果。修复：`selection = self._selection` 取一次引用再读两字段（`payload`/`_flush` 同改）；更彻底是给变更+落盘加 `threading.Lock`，前端切换期间禁用触发器串行化请求。
2. **（v1.28）回滚失败告警写入无人消费、且会被下一次 reload 清空的列表——助手文件损坏不可观测**（`agent/assistant_registry.py:164-165` 追加到 `self.warnings`；`:39-41` 任何后续 `reload()` 都 `warnings.clear()`；`runtime.py:50` 仅启动时消费一次）。磁盘写入失败且回滚也失败时 `assistant.yaml` 可能截断/半程，下次 reload 该助手从列表消失，告警先被抹掉或永远无人读取；用户只看到一次 500，之后助手无声消失。修复：告警改 `bus.emit` 或 logging（运行期可见）或经 API 暴露；补回滚失败路径的显式断言。
3. **（v1.28）`assistant.yaml`/`persona.md` 非原子原地覆写，进程中断可留截断文件且助手无声消失**（`agent/assistant_registry.py:141、147-150、159-160` 全部 `Path.write_text` 直接截断覆写；回滚只覆盖 Python 可捕获异常）。对照 v1.31 为 `llm_providers.json` 引入的「临时文件 + `os.replace`」，助手定义这一核心文件反而没有同等保障。修复：两处写入改同目录临时文件 + `os.replace`（Windows 下原子性可用），顺带缩小上一条的触发面。
4. **（v1.28）registry `reload()` 非线程安全，PATCH/create/delete 与读端并发存在瞬时 404 / 不完整列表窗口**（`agent/assistant_registry.py:39-65` 先 `clear()` 再逐目录重建、无锁；写端经 `asyncio.to_thread` 在工作线程跑 `api/main.py:137,161,180`，读端在事件循环线程直接执行）。两个助手的 PATCH 并发或 PATCH 与 GET 交错时，存在的助手可能瞬时 404、列表瞬时缺项；前端创建/编辑成功后立刻刷新列表恰好命中窗口会把旧列表刷回 UI。该模式 create/delete 既有，PATCH 高频化放大了暴露面。修复：进程内 `threading.Lock` 包住 clear+重建，或构建新 dict 后单次赋值替换。
5. **（v1.28）API PATCH 允许空白显示名落库，与 CLI 拒绝行为口径不一致**（`api/models.py:16` 仅 `min_length=1`，`"   "` 通过；`agent/assistant_registry.py:143-144` 无 strip/空校验；对照 `agent/__main__.py:124-126` CLI edit 显式拒绝空白 name）。直连 API 写入空白名后选择器出现空白条目；架构 §5.4 称 CLI 与 API 同语义，实际一端 400 一端 200。修复：在 registry 收口 `name.strip()` 为空即拒绝，两端自动对齐并补测试。
6. **（v1.30）画像文件编码损坏时 GET/PUT 双 500，且 API 层无自愈途径**（根因 `memory/long_term.py:63`——`replace_profile` 的 `read_text` 在 try 块之外，`UnicodeError` 直接上抛；`api/main.py:53-68` `_raise_http` 无 UnicodeError 分支）。架构定位 profile.md「白盒可手改」，Windows 记事本存成 ANSI/GBK 是现实场景；此后「记忆画像」对话框打开即 500，PUT 也 500（写覆盖坏文件前先读原文就炸），用户无法经 UI 自救只能去文件系统手工处理。recall 本身有降级（有测试），GET/PUT 无覆盖。修复：读原文失败视为 `original=None` 继续写入（正是用户要的「覆盖修复」）；GET 端点把 UnicodeError 映射为带指引的 400。
7. **（v1.30）沉淀提取调用在请求生命周期与助手锁内同步执行，最坏拖住终态事件与助手锁约 120–240 秒**（`agent/runtime.py:616-625、751-760`——`await self._consolidate_chat_memory(...)` 在 `recorder.finish_task("succeeded")` 之前、锁 finally 释放（`:807-808`）之内；放大器 `agent/llm.py:125-144`——`chat_text` 无 `stream_chat_turn` 那样的显式 timeout 包裹，仅受 client 默认 120s 约束，且 json_mode 被拒后**再发一次**完整调用）。回复 token 已流式播完，但 `task_done` 终态要等沉淀返回才发；期间该助手运行锁一直被持有，新任务/聊天/画像 PUT 一律 409——用户看到「回复已出但任务一直不结束、助手被占死」。文档声明「不得影响本轮已交付的聊天回复」对 token 成立、对终态与锁可用性不成立。修复：沉淀移出关键路径（锁释放后 `create_task` + 独立短超时），至少给 `chat_text` 包 `asyncio.timeout` 并禁用 json 回退重试。
8. **（v1.30）「写入失败画像保持原状」契约与实现不符：多条沉淀中途失败时部分条目已落库**（`agent/runtime.py:408-424` 循环内逐条 `memorize` 无事务性；契约 `docs/architecture/phase1-architecture.md:831` §9 故障表；工作条目文案 `runtime.py:422`「本轮记忆沉淀失败，已跳过」同样暗示零写入）。第 N 条失败时前 N-1 条已持久化并注入后续所有轮次，而记录声称「已跳过、画像保持原状」，可观测性与实际状态相反。修复：文案如实（「已写入 k/N 条，其余跳过」），或失败即中止并如实记录；§9 措辞同步收紧。

---

## P3 — 可优化 / 观察项

### v1.28（助手 persona 可写可编辑）

1. **显式 `null` 字段被静默当作「未提供」（200 no-op），契约未写明**（`api/main.py:159` `exclude_unset=True` + `api/models.py:15-17` 三字段均可 null + `assistant_registry.py:121` 三 None 报错仅键全缺时触发）：`PATCH {"name": null}` 返回 200 且无变化，调用方误以为改成功。建议模型禁 null 或端点检测「键出现但值为 None」返回 422，或在架构 §5.9 明示等价语义。
2. **CLI persona 无 50,000 字符上限，与 API 口径不一致**（registry 层无长度校验，仅 `api/models.py` Pydantic 层有；`agent/__main__.py:96-100,127-132` 直通）。CLI 可把 5MB persona 写入并注入 system prompt。建议上限下沉 `registry.create/update` 单处收口；顺带给非 UTF-8 persona-file 报「需 UTF-8 文本」的可读提示（当前裸 traceback 摘要）。
3. **persona 无净化且 Loop/选区改写链路无预算裁剪，超长 persona 使任务在 LLM 上游报错失败**（消费点 `agent/loop.py:63,144,169`、`agent/runtime.py:280` 原样注入，不经过 `agent/context.py` 预算）。50,000 字符合法 persona 撑爆上下文时任务 failed、报错来自 LLM API，用户难以关联原因。建议任务启动时对 persona token 估算超限发指因 warning；控制字符过滤与「超长可能上游失败」明示进 §4.2。
4. **`persona_file` 键可指向助手目录之外**（`agent/assistant_registry.py:131` 绝对路径整体替换、`../` 可越界；同型 `:49` reload 既有）。需本地手改配置才触发、与用户同信任级，但属 id 校验同类的漏网。建议 `resolve().is_relative_to(directory)` 校验，越界按损坏配置拒绝。
5. **前端 persona 输入无字数提示，maxlength 静默截断粘贴**（`AssistantDialog.vue:71`）。粘贴超 5 万字符被浏览器静默截断（UTF-16 口径，只会更严，不引发 422），用户可能不知情丢尾部。加「N / 50000」计数即可。

### v1.29（加固批次）

6. **架构 §5.7 正文截断标注口径未随 v1.29 代码同步，与实现相反**（`docs/architecture/phase1-architecture.md:540` 仍写「追加原始长度标注」；代码 `agent/work_log.py:80,106,121` 与 changelog 现行口径均为「脱敏后 N 字符」）。backlog 恰恰指引读者以 §5.7 为现行契约，形成正文与实现相反的分叉。改一句话即可。
7. **「终态后 start 拒绝」契约只写在 changelog，§5.4 正文未补**（`:420`；实现 `agent/work_log.py:179-182`）。补一句终态守卫契约（含经 `note` 的间接调用）。
8. **`finish_task` 先置终态旗标后落库，落库中途失败时补偿被幂等守卫吞掉**（`agent/work_log.py:327-329` 旗标在落库前；`runtime.py:626/761` 失败进 except → `:790-791` 补偿 `finish_task("failed")` 因旗标已置静默 return）。本地 SQLite 故障时任务报 failed 但工作日志终态行缺失，只能靠对账补 `interrupted`（与真实结局不符）。窗口前置存在，与本守卫同域，建议顺手把旗标移到终态行成功落库之后。
9. **`_finalize_write_intent` 意图缺失分支静默成功，理论上可造成「磁盘已写、DB 未推进」假成功**（`memory/projects.py:690-692` 缺失分支 `commit(); return []`；消费方 `:1770-1774`）。需 30s 同进程宽限过期 + 竞争写者删意图行的复合竞态，正常路径不可达；一旦触达，accept 报 200 但 hunk 仍 pending、无告警。本提交把旧 `None`（会一路炸到前端 `.length`）改 `[]` 已是改善。建议该分支改抛 `StorageRecoveryPendingError` 或至少 warning。
10. **并发对账回归测试的「并发」被 store 实例锁串行化；`reconciled_task_ids` 语义是「尝试过」而非「实际补写」**（`tests/test_work_log.py:886-935`；根因 `memory/store.py:310-316` 全程持锁、`api/main.py:642-646` 已终结任务静默 no-op 后仍 append，测试以 `in ([], ["orphan-race"])` 放行）。端点层幂等收敛断言扎实，但不是真正双事务竞争。可在 backlog 记一笔：让 `interrupt_work_task` 返回是否实际插入。

### v1.30（助手记忆系统）

11. **沉淀期间的取消会把已成功交付的轮次记为 interrupted/failed**（`runtime.py:416` `except Exception` 不捕获 `CancelledError` → 外层 `:770-787`）。提取 await 中客户端断开 → 终态 interrupted，但回复已完整送达落库，工作记录与任务终态与体验不符。在 `_consolidate_chat_memory` 内对 `CancelledError` 静默放行，或随 P2-7 挪出关键路径后自然消失。
12. **GET /memory/profile 与运行中写入并发可读到半程文件**（`api/main.py:187-197` GET 不取锁；`memory/long_term.py:66` `write_text` 先截断后写）。用户可能读到空/半截画像并保存固化。GET 共享锁读取或 `write_text` 改临时文件+`os.replace`（顺带改善 P2-6）。
13. **普通任务启动无条件播报「已注入助手记忆」，零命中也播**（`runtime.py:190` 无条件；对照 chat 路径 `:525` 仅命中或降级时建条目）。每任务一条「0/0/0」噪音，两路口径不一致。对齐 chat 门控。
14. **信号门槛宽泛：风格/语气类**提问**每轮多付一次提取调用；多语言指令不覆盖**（`agent/chat_memory.py:22-29`）。「这篇文章的风格是什么？」命中「风格」→ 每轮一次非流式调用（成本受控、模型大概率返回空，属 §5.4 明示的设计内取舍）；英文「remember that…」不触发。可加疑问语气排除或接受现状。
15. **MemoryProfileDialog 无 Escape 关闭、无焦点陷阱；无助手时可打开**（`MemoryProfileDialog.vue:57-93`；入口 `App.vue:609` 无 disabled 守卫）。aria-modal 但 Tab 可穿透，与 ProjectDialog/AssistantDialog 既有缺口一致、非本提交首创；零助手态打开得到「资源不存在」。与其他对话框统一补 Escape + 焦点陷阱；无助手时禁用入口。

### v1.31（模型/提供商切换）

16. **短 API Key 掩码泄漏比例过高**（`agent/llm_providers.py:48-50`——≤8 位才整体掩码，否则回显前 3 后 4）：9 位 key 暴露 7 位、12 位暴露 7/12。阈值提到 ≤12（或 ≥16 才回显前后缀）。
17. **`_flush` 无 fsync；崩溃残留的 `.llm_providers-*.tmp`（含明文 Key）从不清理**（`agent/llm_providers.py:273-281`）。断电后配置可能空/半截（恢复 = 删除重建，丢全部手工提供商）；残留 tmp 含明文 key 长期留磁盘（git 风险已由 `*.tmp` ignore 规则兜底，已验证）。`os.replace` 前 fsync；启动时清扫孤儿 tmp。
18. **落盘失败后内存与磁盘状态分叉，错误语义与实际效果相反**（`:242-243` add 先 append 后 flush；`:250-251` select 先改指针后 flush）。flush 抛 OSError 时 API 返回 500 但新增已在内存可见、切换实际已生效，用户按报错重试造成困惑。先落盘成功再更新内存，或失败回滚内存。
19. **手改文件的值级校验缺口：temperature 越界抛裸 ValueError 不带文件路径；models 元素无空值/去重校验**（`:157` `_load` 内直接调 `_clean_temperature` 绕过 RuntimeError 包装；`:156` `str(model)` 放行空串/重复）。白盒手改写 `temperature: 5` 启动报错无路径指引，违背「显式报错并指向文件路径」的自我承诺。包装成带 `self.path` 的 RuntimeError；models 复用 `_clean_models`。
20. **`base_url` 仅前缀校验，错误地址到任务运行时才失败**（`:227-228` 只查 `startswith(("http://","https://"))`）。`https://x .com` 可保存成功，首次发任务才收到难懂的 SDK 报错。本地单用户无 SSRF 面（新增时不发请求；探活按钮 = backlog 已登记暂缓，不重报）。建议 urlparse 校验 scheme+netloc。
21. **跨进程不感知：长驻 Scheduler 进程永不重载 `llm_providers.json`，UI 切换对定时任务不生效**（`agent/__main__.py:86-88` 独立构造 Runtime，注册表仅构造时读一次；对照 `llm_providers.py:100-103` 仅文件不存在时 bootstrap）。架构文档「最后写入者胜出」只覆盖写冲突、未覆盖读侧陈旧。至少在架构文档补多进程边界（Scheduler 重启后生效），或按 mtime 重载。
22. **前端三处小缺口**：切换无 in-flight 防护（`AgentPanel.vue:109-116`，P2-1 放大器）；「添加提供商…」未注册进方向键 roving 循环（`ModelPicker.vue:160`，键盘用户只能 Tab 到达，与主题菜单不完全对齐）；422 数组 detail 提示退化为状态文本（`client.ts:20-31`，与 P1-1 的放大器同一处）。
23. **架构 v1.31 三处措辞与实现不符**（`docs/architecture/phase1-architecture.md:60、:416`）：「每个任务在**获锁后**解析一次快照」——实现是获锁前解析（`runtime.py:174` vs `:184` 等；行为无碍但口径不对）；「Windows icacls 限当前用户」——实现还授 `SYSTEM:F`（`llm_providers.py:62-66`）；「文件损坏显式报错并指向文件路径」对 temperature 越界不成立（见第 19 条）。随下版顺手更正。

---

## 已核实无问题的关键面

1. **跨助手隔离红线（逐方法核对全部干净）**：v1.28 GET/PATCH/DELETE assistants 端点只取路径 id；v1.30 recall 链路——`profile_path` 经 `validate_id`（`memory/validation.py:6-12`，无点无斜杠）、`short_term` 六个检索全部 `WHERE assistant_id = ?`、GET/PUT profile 先 `assistants.get` 404 再操作；v1.29 补的跨助手 change set 404 测试走「已注册但无权限」路径。有 `test_recall_trace_cross_assistant_isolation`、`test_profile_isolated_per_assistant` 等实质断言。
2. **SQL 纪律**：四个提交新代码零 SQL 出 `memory/`；`chat_memory.py` 纯内存管线，全部持久化经 MemoryStore 门面。
3. **密钥边界（硬性规则 4/11 修订后的实际落实）**：GET 载荷只输出 `api_key_hint` 且有「明文只存在于本地文件本身」的反向断言（`test_llm_providers.py:167-178`）；前端 API Key 输入 `type="password"` + `autocomplete="off"`、provider 数据不进 localStorage（全仓仅 theme.ts 用 localStorage）；`llm_providers.json` 已 gitignore 且未被跟踪（`git ls-files` 验证）；报错信息只含提供商名与模型名，不含密钥。
4. **温度收敛彻底**：全仓 grep 确认 loop 四节点、planner、chat_memory、runtime 全部调用点显式传快照温度；`llm.py` 的 0.3 仅为签名缺省、实际调用均显式传参。
5. **按任务快照路由一致性**：三路径均在入口解析一次三元组并贯穿历史压缩、记忆沉淀、两轮流式；`_llm_override`（测试替身）优先于缓存客户端，`runtime.llm` property 门面与既有三个测试文件注入路径兼容；v1.31 顺手把 v1.30 沉淀调用改为消费任务级快照，消除了运行中切换提供商后沉淀用错模型的漂移隐患。
6. **v1.29 三条契约变更的所有调用方同步**：`_finalize_write_intent -> list[str]` 全仓 3 个调用点类型假设一致（顺带消除 `null.length` 前端崩溃链）；终态守卫当前无可达触发路径且与「落库失败降级 warning」机制交互安全（`done()`/`_note_persist_failure` 不经 `start`）；watchTask 退避复位/看门狗/游标续传（`after_seq`/`Last-Event-ID`）语义完整，重连耗尽永久放弃 + 明确 UI 提示，「不设总次数上限」为 backlog 明文登记的取舍。
7. **先脱敏后截断顺序**（v1.29 标注改动处）：detail/args/result 三处均先脱敏后截断，不存在「截断把敏感值切一半再漏检」路径，标注 N 与内容自洽。
8. **page_size clamp（≤100）与前端分页无冲突**：API 层 `Query(le=100)` 先行 422，前端 `reconcileChanges` 恒用默认 pageSize=20、终止条件基于 items.length 与 total，phase9 分页缺陷无回归。
9. **YAML load→mutate→dump 字段完整性**（v1.28）：update 只改 name/description 两键、`safe_dump(allow_unicode=True, sort_keys=False)` 整体回写，skills/created_at/persona_file 及自定义键全保留、键序保持，round-trip 一致，有专门测试。
10. **运行锁边界与删除一致**（v1.28/v1.30）：PATCH 与 PUT profile 同 `acquire_lock` 模式、acquire 失败不进 try/finally（无误释放）、锁释放/无残留有断言；PATCH 持锁期间任务/删除/再 PATCH 均 409。
11. **运行中切换不打断在跑任务**：任务启动时持 Assistant 快照与提供商三元组快照，reload/切换均重建新对象而非原地修改。
12. **v1.30 沉淀管线健壮性面**：坏 JSON/非法 kind/超量/超长在启发式路径均有收口且有测试；failed/interrupted 不沉淀（调用点均在成功分支）；`CHAT_MEMORY_CONSOLIDATION` 关闭在信号扫描之前 return、真零成本；「已注入助手记忆」工作条目只播计数与降级标记，无记忆正文外泄，落库走既有脱敏。
13. **键盘导航（ModelPicker 与 ThemePicker 同构）**：打开聚焦当前项（正确处理稀疏 ref 空洞）、ArrowUp/Down 循环、Esc、点击外部关闭；对话框拒绝不关框保留内容，组件级+集成级双覆盖。

---

## 文档同步核查

- **架构文档**：v1.28/v1.29/v1.30/v1.31 四个 changelog 块与 §4.2/§5.4/§5.7/§5.9/§5.10/§9 对应条目逐条对照实现，总体一致且表述克制（如「含与画像已有等价记录去重」被准确限定为「提示词要求」）。偏差已逐条列入发现：§5.7 截断标注口径相反（P3-6）、§5.4 终态守卫正文缺失（P3-7）、§9「画像保持原状」与部分写入实现不符（P2-8）、v1.31 三处措辞（P3-23）；另有 §4.7/记忆注入段未声明「注入内容自身无上限」——P1-3 的制度缺口。
- **AGENTS.md/README**：四个版本的能力描述、基线数字（306→330→344 Python；142→154→174 前端）逐版递增且与实跑一致；硬性规则 4/11 的密钥边界修订与 `llm_providers.json` 实现相符。
- **backlog**：「加固批次」整节移入已完成且五项描述与代码一一相符；phase7 P3-4、phase8 P3-5、phase7 P3-9 三条随 v1.30 落地移除，经核实确已实现；v1.31 探活按钮如实登记为暂缓。本次发现与既有暂缓项唯一重合点为 phase8 P3-3（非 JSON 纯文本值级扫描），已在 v1.29 面核对时标明。
- `.env.example` 两版增量（CHAT_MEMORY_CONSOLIDATION、提供商占位）均无真实密钥形状。

---

## 验证记录

- Python 全量：`python -m pytest tests -q -p no:cacheprovider --basetemp D:\test_agent\pytest-temp-full` → **344 passed**（19.9s）。
- 前端：`npm test` → **15 文件 174 passed**；`npm run typecheck`（vue-tsc -b）→ 通过。
- 与 AGENTS.md/README 声明基线（Python 344/344、记忆隔离 11/11 含于全量、前端 174/174）账实相符。
- 审查过程：四路并行按提交深审 → 主审对 P1-1/P1-2/P1-3 与 P2-1 的关键断言逐条亲验（`api/models.py:17`、`agent/chat_memory.py:15-18,22-29`、`agent/runtime.py:389-395,476-506,616-625`、`memory/long_term.py:19,37-52,55-61`、`config/settings.py:41`、`agent/context.py:60-61`、`agent/llm_providers.py:130-175,195-215,240-281`、`web/src/components/AssistantDialog.vue:45`、`web/src/api/client.ts:20-31`），全部属实；P1-1 另有两路独立发现相互印证。

## 处理建议

1. **建议随下版立即处理（一行级，成本低）**：P1-1（回退 min_length 或前端省略未变更字段 + 回归测试）、P2-1（撕裂读快照引用 + 锁）、P3-6/P3-7/P3-23（文档一句话更正）。
2. **建议下一版本集中收口记忆管线**：P1-2（直达路径截断+去重）、P1-3（注入侧按预算裁剪 + 有效上限下调）、P2-7（沉淀挪出锁内关键路径）、P2-8（失败文案如实）。这四条共同构成「记忆写入有界、注入有预算、失败可观测」的闭环。
3. **建议登记 backlog**：P2-2/P2-3（助手文件原子写与可观测性，可与 P3-12 一并以「临时文件+os.replace」统一收口）、P2-4（reload 线程安全）、P3-2（CLI persona 上限下沉）、P3-21（多进程读侧陈旧边界）。
4. **P2-5/P2-6 与 P3-10/P3-16/P3-17/P3-18/P3-19/P3-20** 属低成本边界加固，可并入任意一次「加固批次」。
5. 按 phase9 惯例，本报告的处理结果应在下个版本以「处理结果记录」小节回写本文件，并同步更新 `docs/README.md` 的审查记录表。

---

## 文档口径处理结果记录（2026-09-02，v1.32）

按处理建议第 1 条完成纯文档口径修正（架构文档升版 v1.32，不改任何代码行为）：

- **P3-6 关闭**：架构 §5.7 截断标注改「脱敏后长度标注（v1.29 口径）」。
- **P3-7 关闭**：架构 §5.4 工作记录段落补终态守卫契约（置位终态后 `start`/`note` 显式拒绝，含经 `note` 的间接调用）。
- **P3-23 部分关闭**：「获锁后解析快照」改「获锁前」（v1.31 变更行与 §5.4 两处）；「icacls 限当前用户」补「与 SYSTEM」（两处）；第三小项（temperature 越界报错不带文件路径）属代码侧修复（P3-19），随代码修复闭环，文档契约表述保持不变。
- **P2-8 文档分支关闭**：§9 故障表改为如实描述（提取调用失败画像保持原状；`memorize` 逐条写入，中途失败已写入条目保留、其余跳过）；工作条目文案如实化（「已写入 k/N 条」）保留为代码侧待修项。
- 顺带修正两处陈旧版本引用（原停留在 v1.28）：`docs/README.md` 现行依据表、`docs/guides/new-session-prompt.md`（其测试基线 299/299、142/142 与密钥规则旧表述一并同步至 v1.32 现状）。
- 其余 P1/P2/P3 代码侧修复以本报告为工作清单待排期；观察项 P3-3/P3-10/P3-14/P3-15/P3-21 已登记 `docs/guides/backlog.md`「phase10 复审观察项」。

---

## 处理结果记录（fix/phase10-review 分支，v1.33）

代码侧修复于 2026-09-02 在 `fix/phase10-review` 分支一次完成（架构文档随本次修复升版 v1.33，单提交并实跑全量回归）。测试基线 344→377（Python）、174→179（前端），隔离 11/11 常绿。逐条处置：

### P1（3/3 已修复）

| 项 | 处置 | 决定与理由 |
|---|---|---|
| P1-1 | v1.33 已修复 | 选择方案 (a) 回退 `min_length=1`（报告推荐项）：空描述语义与创建入口对齐，PATCH 部分更新语义保持简单，不引入「前端按字段 diff 省略」的隐式逻辑；同时让 `client.ts` 对 422 数组 detail 取首条 `msg`（P3-22c 同点）。补「编辑空描述助手可保存」回归测试 |
| P1-2 | v1.33 已修复 | 直达路径：内容超 120 字或含换行不直达、交启发式提取由模型裁决（截断会产生无意义片段，报告两选项取「拒绝直达」）；疑问语气（结尾 吗/呢/？/?）不直达；直达与启发式两条路径落库前均与画像做规范化（去空白）比对去重。**撤销部分例证**：报告所举「你记住我要写什么了吗」因 `re.match` 锚定串首、首字「你」不匹配指令词，实际不触发该路径——缺陷例证不准确，但「记住……吗？」类疑问形态确可匹配并误存，缺陷实质成立，按真实缺陷修复 |
| P1-3 | v1.33 已修复 | 注入记忆块按 `chat_context_token_budget` 的 1/3 份额裁剪（chat 与普通 run 双路径，复用 `clip_content_to_token_budget`；预算 0 即关闭压缩时不裁剪）；记忆裁剪与「文档被挤到 0」均发指因 warning；提取 prompt 画像截取前 8,000 字符。选择「注入侧份额裁剪」而非 Memory 层双上限：存储白盒上限（50,000）不变，注入有效上限随预算自适应 |

### P2（8/8 已修复）

| 项 | 处置 | 决定与理由 |
|---|---|---|
| P2-1 | v1.33 已修复 | 三层同修：`_flush`/`payload`/`selection`/`resolve` 对当前选择取单一快照引用（以可注入替身模拟「两次属性读之间并发切换」的确定性回归测试）；变更+落盘以进程内 `RLock` 串行；前端切换 in-flight 期间禁用触发器 |
| P2-2 | v1.33 已修复 | 回滚失败改经 `logging.warning`（含助手 id 与文件路径，运行期可见）：registry 无 bus 引用，logging 是 CLI/API 两端通用的运行期通道，`self.warnings` 保留为启动扫描专用 |
| P2-3 | v1.33 已修复 | `assistant.yaml`/`persona.md` 全部写入（create/update/回滚）统一同目录临时文件 + `os.replace`，对齐 v1.31 `llm_providers.json` 写入标准 |
| P2-4 | v1.33 已修复 | 报告两个选项合并采用：整体重建后单次替换 `_assistants`（读端不进清空窗口）+ `RLock` 串行写端与 reload；`get` 改单次字典查找消除「先判在再取」竞态；补并发读回归测试 |
| P2-5 | v1.33 已修复 | registry `create`/`update` 单点收口空白显示名（报告建议方案），CLI/API 两端自动对齐，直连 API 写空白名 400；create 同步收口（同型缺口顺手修） |
| P2-6 | v1.33 已修复 | `replace_profile` 读原文遇 `UnicodeError` 按无原文继续写入（正是用户要的覆盖修复）；GET 把编码损坏映射为带指引的 400；v1.33 原子写进一步消除 GET 读到半程文件的窗口（P3-12） |
| P2-7 | v1.33 已修复 | 选择报告的「至少」方案：提取调用以 `CHAT_MEMORY_EXTRACTION_TIMEOUT_SECONDS`（默认 30 秒）独立限时，最坏 120–240 秒收敛到 30 秒内；未选「锁释放后 create_task 分离」——分离会让「已沉淀助手记忆」条目出现在终态之后（实时流已关闭、只能刷新可见），且需给记录器引入跨实例序号续接，契约收益不抵复杂度 |
| P2-8 | v1.33 已修复（代码侧；文档分支 v1.32 已闭） | 文案如实化：「本轮记忆沉淀部分失败：已写入 k/N 条，其余跳过（不影响回复）」；已写入条目以进度条目呈现（§9 v1.32 如实化口径的实现补齐） |

### P3（23 条：16 已修复、5 暂缓、2 文档分支已闭）

| 项 | 处置 | 说明 |
|---|---|---|
| P3-1 | v1.33 已修复 | 报告三选一取「端点检测显式 null 返回 422（列出字段名）」：保留 `exclude_unset` 部分更新语义，显式 null 作为调用方错误尽早暴露 |
| P3-2 | v1.33 已修复 | 上限下沉 registry 单点收口（create/update）；CLI 非 UTF-8 persona-file 给「persona 文件必须是 UTF-8 文本：<路径>」可读报错 |
| P3-3 | 暂缓 | 已登记 backlog「phase10 复审观察项」，维持 |
| P3-4 | v1.33 已修复 | `resolve().is_relative_to(directory)` 校验，越界按损坏配置拒绝（update 400；reload 记 warning 跳过，不阻断启动） |
| P3-5 | v1.33 已修复 | 对话框 persona 输入下方「N / 50000」计数 |
| P3-6 | 文档分支 v1.32 关闭 | — |
| P3-7 | 文档分支 v1.32 关闭 | — |
| P3-8 | v1.33 已修复 | 终态旗标移到终态行成功落库之后；finish_task 全程同步，旗标后置不产生 start/note 竞态窗口，v1.29 守卫语义不变；补落库失败→补偿重试回归测试 |
| P3-9 | v1.33 已修复（契约修订） | 缺失分支改抛 `StorageRecoveryPendingError`（API 409），消除「磁盘已写、DB 未推进」假成功；该分支需复合竞态才可达，正常路径不受影响；同步改写 phase7 P3-5 的既有契约测试并在架构 §4.7/v1.33 变更行注明契约修订 |
| P3-10 | 暂缓 | 已登记 backlog，维持 |
| P3-11 | v1.33 已修复 | 回复已交付后沉淀中的 `CancelledError` 由沉淀函数吞掉并记 info，本轮照常 succeeded（未随 P2-7 挪出关键路径，故按报告第一方案在函数内放行） |
| P3-12 | v1.33 已修复 | profile.md 整文替换/首部初始化/超限裁剪重写统一原子替换 |
| P3-13 | v1.33 已修复 | run 路径零命中不再播报，与 chat 门控对齐；命中播报追加「超出注入预算已截断」标记（P1-3 随行） |
| P3-14 | 暂缓 | 已登记 backlog，维持 |
| P3-15 | 暂缓 | 已登记 backlog，维持 |
| P3-16 | v1.33 已修复 | 取报告两选项中更保守者：<16 位整体掩码 `***`（短密钥熵低，回显前后缀辨识价值低而泄漏比例高），≥16 才回显前 3 后 4 |
| P3-17 | v1.33 已修复 | `os.replace` 前 fsync；启动清扫孤儿 tmp——超 60 秒宽限期才删（宽限期内的 tmp 可能属于并发进程在写，避免误删），补新旧 tmp 分别处置的回归测试 |
| P3-18 | v1.33 已修复 | 先落盘成功再更新内存（add/select 同改），补落盘失败内存不变的回归测试 |
| P3-19 | v1.33 已修复 | 温度越界/模型名空白包装为带 `self.path` 的 RuntimeError；models 复用 `_clean_models`（空白拒绝、去重）；补回归测试 |
| P3-20 | v1.33 已修复 | urlparse scheme+netloc 校验并拒绝空白字符（`https://x .com` 形态由空白规则拦截）；补回归测试 |
| P3-21 | 暂缓 | 已登记 backlog，维持 |
| P3-22 | v1.33 已修复 | 三处全修：selectProvider 置 busy 禁用触发器；「添加提供商…」注册进方向键/Home/End roving 循环；422 数组 detail 取首条 msg |
| P3-23 | 文档分支 v1.32 关闭 | 第三小项（temperature 越界报错不带路径）属 P3-19 代码侧，随 v1.33 闭环 |

### 撤销与报告外说明

- **撤销部分例证（P1-2）**：报告触发场景所举疑问句「你记住我要写什么了吗」实际不匹配 `_EXPLICIT_COMMAND`（`re.match` 锚定串首，首字「你」无法命中指令词分支）；经现场核对，缺陷以「记住……吗/呢/？」形态真实存在，修复针对该形态。除此之外未发现误报，23 项发现全部账实相符。
- **报告外顺手修（同文件/同主题）**：`selection()`/`resolve()` 一并快照化（P2-1 同域）；`append_profile` 的首部初始化与超限裁剪重写一并原子化（P3-12 同域）；registry `create` 的空白显示名一并拒绝（P2-5 同域）。无关新缺陷未发现，backlog 无新增登记。
- **架构文档升版记录**：v1.33（本次修复单次升版，覆盖提供商配置边界与助手编辑契约、记忆管线收口、助手文件一致性、边界收尾四组改动）；`CHAT_MEMORY_EXTRACTION_TIMEOUT_SECONDS` 为新增可配项（默认 30 秒）。
- **验证**：实跑 `python -m pytest tests -q -p no:cacheprovider --basetemp D:\test_agent\pytest-temp-full` 全绿 + `tests/test_memory_isolation.py`（pytest-temp-isolation）常绿 + `web/` 下 `npm test`、`npm run typecheck` 通过；收尾另跑 `npm run build` 通过。最终基线：Python 377/377、隔离 11/11、前端 179/179。
