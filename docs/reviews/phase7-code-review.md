# 阶段 7 代码审查报告

> 审查对象：`writing-agent/` 阶段 6 审查基线（`f35495b`，v1.18）之后的全部已提交改动，即 v1.19 项目聊天持久化工作记录、v1.20 多 hunk change set 与逐 hunk 审查、v1.21 phase6 复审加固、v1.22 写作 IDE 多主题界面，共 6 个提交（`7ca426e`、`b406f01`、`fe017d8`、`d9b219e`、`8192ae8`、`f80031d`），50 个文件、+4728/−924 行
> 审查日期：2026-08-17
> 审查方式：五路并行静态走读（工作记录 / hunk 模型 / phase6 修复复核 / 前端主题与 hunk 审查 / 文档与敏感信息横向）+ 关键证据逐条二次核验。按用户要求，本次未改动任何代码，也未重跑 pytest/vitest（测试已由用户跑过并全绿）
> 环境：Windows 11 / 与 AGENTS.md 声明环境一致

---

## 总体评价

先说做得好的部分：

- **工作记录的终态与隔离设计扎实**：`(assistant_id, project_id, task_id) WHERE kind='task'` 部分唯一索引 + `INSERT OR IGNORE` 使正常终态与对账补写共用同一幂等约束（`memory/project_chat.py:66-67、484-502、571-603`）；`CancelledError` 与 `except Exception` 两条退出路径分开处理，终态落库失败只记 warning 不掩盖原始错误（`agent/runtime.py:522-543`）；工作记录读写全部经 `_session_row` 三元组过滤，会话/项目/助手级联清理齐全；`work_item` 事件经 ContextVar 携带 task_id 进入 broker 滑窗，断线可补发（`api/tasks.py:50-59`）。
- **199+1 上限实现精确**：第 200 位固定为溢出摘要、按被省略类型合并计数、任务终态不受限（`agent/work_log.py:175-197、217-237`），delta 永不落库（`:147-149`），与架构 §5.7/§9 逐条一致。
- **hunk 拆表迁移真单事务、幂等、可回滚**：`BEGIN IMMEDIATE` + 失败整体 rollback，触发条件为 `task_id` 列缺失故成功后不再重跑，历史记录生成确定性 `legacy-<id>` 合成值避免 NULL 绕过唯一索引（`memory/projects.py:325-387`）。
- **接受原语与并发边界清晰**：接受单个 hunk 沿用写意图三段式，`UNIQUE(assistant_id, project_id, document_id)` intent 使后到者 409；版本推进 + hunk 状态 + 他组 stale 在同一 finalize 事务（`projects.py:598-657、1425-1506`）；`create_change_set_hunks` 单事务完成唯一匹配定位、排序、不重叠、≤100/≤1MiB 校验，任一失败整批回滚、无半成品行（`projects.py:1230-1299`）。按文档分组提交确实修复了「同文档多处修改整批失败」缺陷。
- **phase6 修复全部属实且被测试固化**：P2-1 预算兜底、P3-2 助手 id 对齐、P3-3 LOCAL_PROXY 降噪、P3-6 pending 作用域、P3-9 README 链接、P3-10 assert 改显式 raise，六项逐一核验在位；P3-1 经 v1.19 工作记录实质解决；其余暂缓项均已在 `docs/guides/backlog.md` 登记归口，无「只改文档不改行为」的虚假修复。
- **主题存储链路防护完整**：localStorage 读写处处 try/catch，「仅显式选择才写入」经测试断言属实（`web/src/theme.ts:39-53、77-81`，`theme.test.ts:73`）；matchMedia 只在 main.ts 注册一次；ThemePicker 监听成对注销。
- **仓库卫生保持**：全区间 diff 无真实密钥，`.env`/`data`/构建产物忽略有效；测试基线账面精确吻合（按 test 函数清点：208 个函数 + parametrize 展开 11 例 = 219，前端 `it/test` 恰 97，与 AGENTS.md/README/new-session-prompt 三处声明一致）。

本阶段发现 **0 个 P0、2 个 P1、5 个 P2、12 个 P3**。没有数据损坏或越权问题；迁移、并发写意图、级联清理、seq 去重等关键路径经走读确认安全。两个 P1 均为「已声明的契约机制在生产路径上未实际生效」：工作记录脱敏被整体绕过、五套主题的语法高亮变量全部是死选择器。

测试现状（引自 AGENTS.md 声明基线，用户已跑过，本次未重跑）：

```
Python 219/219 · 记忆隔离 10/10 · 前端 97/97 · vue-tsc 与生产构建通过
本次新增覆盖：test_work_log.py（约 20 例）、test_change_hunks.py（约 25 例）、
test_chat_context.py（+3 例）、前端 ThemePicker/theme/App/AgentPanel/DocumentEditor 等 +27 例
```

---

## P0 — 阻断验收

未发现。

## P1 — 应修复

### 1. 工作记录脱敏在生产路径被整体绕过：args/result 均为 JSON 字符串，从未经过 redact

**位置**：`agent/work_log.py:32-42、48-51、60`，`agent/runtime.py:411、435`

`redact()` 只对 dict/list 递归，其他类型原样返回；`summarize_args` 对非 dict/list 直接 `str(args)`、`summarize_result` 对 str 原样使用。而 runtime 唯一的两处调用传入的都是字符串：`args=call.arguments` 是 LLM 工具调用的 JSON 字符串（`agent/llm.py` 中 `arguments: str`），`result=output` 是工具返回的 JSON 字符串（runtime.py:436 紧随 `json.loads(output)` 可证）。于是架构 §5.7 与 AGENTS.md 宣称的「参数/结果脱敏截断」在当前生产路径上是死代码——`json.loads` 在 runtime.py:414 其实已经做过，但没有用于工作记录。

**失败形态**：当前唯一工具 `propose_project_edits` 的参数是文档 hunk、结果是 change_set_ids，暂无凭据泄漏的实际通道，损害有限；但脱敏是架构承诺的唯一防线，一旦接入任何携带凭据参数的 MCP 工具，`api_key`/`token` 字段将明文落库并走 SSE。更隐蔽的是 `tests/test_work_log.py:236` 的脱敏用例只传 dict，恰好绕开该缺陷，测试全绿反而提供了错误信心。

**修复建议**：`summarize_args`/`summarize_result` 对字符串先尝试 `json.loads` 再 `redact`，解析失败回退原文；或让 runtime 传解析后的 dict。补字符串形态的脱敏用例（RED → GREEN）。

### 2. CodeMirror 语法高亮未真正主题化：五套主题的 `--cm-*` 语法色变量全部是死选择器

**位置**：`web/src/styles.css:49-53、97-101、144-148、191-195、239-243、319-323`，`web/src/components/DocumentEditor.vue:178-179`

编辑器仅引入 `basicSetup` + `markdown()`，高亮来自 basicSetup 内置的 `syntaxHighlighting(defaultHighlightStyle, {fallback:true})`。已核实 `defaultHighlightStyle`（`node_modules/@codemirror/language/dist/index.js:1803` 起）全部用内联样式 spec（`color`/`fontWeight`/`textDecoration`）定义，@lezer/highlight 会为其生成匿名类名（`ͼN`）并注入硬编码颜色，DOM 上不会出现 `.cm-heading`/`.cm-link`/`.cm-keyword` 等语义类。因此 styles.css 为五套主题精心定义的 `--cm-*` 变量与 `.cm-heading { color: var(--cm-heading) }` 等规则（:319-323）永不命中。

**失败形态**：语法 token 颜色与所选主题无关——深色主题（墨夜/海湾）下仍是浅色优化的硬编码色，对比度差；v1.22 宣称的「深色主题覆盖 CodeMirror」只对行号槽/光标/选区等真实类生效，语法色这一半未达成，且 CSS 表面上完整、不易被察觉。

**修复建议**：注册自定义 `HighlightStyle`，用显式 `class:` 映射（如 `{tag: tags.heading, class: 'cm-heading'}`）后经 `syntaxHighlighting` 引入，使 token 落到现有 CSS 变量上；补一条断言 DOM 类名的编辑器测试。

---

## P2 — 建议修复

### 1. `reject_change_hunk` 不清理孤儿写意图，崩溃残留会让放弃持续 409

**位置**：`memory/projects.py:1521-1548`（对照 accept `:1436`、保存 `:1034`、创建 `:1322`、读文档 `:925` 均先 `_recover_write_intents`）

accept/save/create/读文档路径前都调用 `_recover_write_intents` 清理死进程的孤儿写意图，唯独 reject 只查 intent 存在性（且按 `change_set_id` 过滤，`:1528-1532`）。若进程在接受该 change set 某个 hunk 的写意图提交后崩溃，孤儿行会让之后对同组任意 hunk 的放弃持续 409「文档正在被写入，稍后再试」，直到用户碰巧打开该文档（`get_document` 触发恢复）才自愈。修复成本极低：reject 前对目标文档先 recover 一次。

### 2. 工作记录明细落库失败会把整轮任务打成 failed

**位置**：`agent/work_log.py:175-194`（`store.add_project_chat_work_event` 无降级），`agent/runtime.py:435`（`done()` 不在保护块内）

中间 `done()` 的落库写入没有 try 保护。工具已成功、change set 已创建之后，若这一次 SQLite 写入失败（磁盘满、句柄异常等），异常一路上抛到 `except Exception` 分支，任务以 failed 收场——留下「pending 修改建议存在 + 任务终态 failed」的部分状态，用户看到的是工具明明成功却报失败。终态写入失败已有「只记 warning 不掩盖原始错误」的先例（runtime.py:526-541），明细写入应同权：catch 后降级为 warning 并可补发一条 warning 工作项。

### 3. 脱敏面过窄：只认键名不扫值，失败 detail 不脱敏也不截断

**位置**：`agent/work_log.py:16、32-42`，`agent/runtime.py:432、535`

与 P1-1 同层的两个缺口：其一，`redact` 只按键名匹配，字符串值内部的敏感内容（如错误报文里内嵌的 `sk-...`）不扫描，而 `token_budget` 这类键名又会被过度脱敏；其二，失败路径的 `detail=str(exc)` 不截断、不脱敏，直接落库并经 `work_item_done`/`failed` 事件走 SSE。异常文本是全集里最可能携带意外敏感内容的载体。建议对 detail 设长度上限，并在修复 P1-1 时一并考虑值级扫描（量级模式按需）。

### 4. 同会话连续多轮时，上一轮已完成的工作记录从视图消失

**位置**：`web/src/components/AgentPanel.vue:416`（`send()` 起始 `liveWork.value = null`），`:75-76、276-277、298`

终态的工作记录挂在 `liveWork` 上，从不并入 `workRecords`（后者仅 loadSession 时填充），发送下一条消息即被置空。第 1 轮完成后记录可见，用户发第 2 轮后第 1 轮记录消失，重载会话才恢复——与「每轮记录插在对应 user 消息之后」的呈现意图不符，也让 v1.19 持久化的价值在连续对话中打了折扣。建议终态时把 `liveWork` 归档进按消息索引的已完成记录列表，新轮开始保留历史。

### 5. 侧栏卡片缺少架构契约要求的导航交互

**位置**：`web/src/components/ChangeDiff.vue:11、14-33`，`AgentPanel.vue:595`；契约见 `docs/architecture/phase1-architecture.md:578`

架构 §5.10 明确「点击卡片打开目标文档，点击具体 hunk 滚动定位」，且「目标文档未打开时，侧栏是唯一入口」。现状 `ChangeDiff` 只 emit apply/reject/regenerate，模板内无任何点击打开/定位逻辑（工作记录 changes 项的打开文档不能替代）。目标文档未打开时，用户无法从卡片跳转进入内联逐 hunk 审查，只剩批量接受/放弃。建议卡片头部与 hunk 块补 openDocument/定位事件。

---

## P3 — 可优化

1. **预算兜底的残留缺口**（`agent/context.py:123-135、:14、:100`）：`_fit_messages_to_budget` 的最终截断分支截后不再复核，且截断标注（约 33 token）加在 allowance 之外；当 allowance 落到 1000 字符地板（system prompt 近占满预算时）超出可达约 1000 token。docstring「保证 prompt 估算恒不超预算」实际是近似成立——默认配置下残留仅约 33 token，不致触发供应商 400，量级无害，但建议把标注计入 allowance、改 docstring 措辞，并给该分支补直接测试（现有三个用例均被首轮 60% cap 解决，未触及此分支）。
2. **旧 schema 死代码 `_row_to_change_set`**（`memory/projects.py:404-410`）：仍按 start/end/original_text/replacement_text 构造，而 `ChangeSetRecord` 字段已改（`:158-168`），任何调用立即 TypeError；重写遗留、当前无调用点，建议删除。
3. **accept-all 中断提示文案残缺**（`web/src/App.vue:319`）：`` `第 ${...}修改建议已失效` `` 渲染为「第 部分修改建议已失效」或「第 修改建议已失效」，疑似删除计数变量的残留，建议改为「部分修改建议已失效，其余建议请逐处确认」。
4. **memory 层分页无 page_size 上限**（`memory/projects.py:1389-1414`）：上限只存在于 API 层（`api/main.py:319-320` `le=100`），绕过 API 的调用方可一页拉全表，建议 memory 层同步 clamp。
5. **`_finalize_write_intent` 返回契约不一致**（`memory/projects.py:578-583`）：注解 `-> None`，实际一处返回 `staled` 列表、intent 丢失分支返回 None；调用方按 list 使用（`:1502、1506、1577`），None 会 TypeError。现路径不可达（活进程的 intent 不会被恢复逻辑清除），仍应修注解并兜底返回空列表。
6. **结果截断标注使实际长度略超文档上限**（`agent/work_log.py:61-65`）：6000+2000 截断后追加标注约 50 字符，实际最长约 8050，与架构「最多 8000 字符」字面略有出入（测试按 ≤8200 断言），建议文档注明「不含截断标注」。
7. **`finish_task` 后不拒绝新的 start**（`agent/work_log.py:110、112、211`）：终态后再 `start` 会复用已占用的 seq 触发唯一约束冲突；当前 runtime 无此调用顺序，纯防御性，建议 finish 后拒绝 start。
8. **ChangeDiff「当前文档」label 回退残留**（`web/src/components/ChangeDiff.vue:17`，`App.vue:51-53`）：`documentLabels` 只来自资源管理器当前选中项目的树，当 Agent 作用域来自活动标签而资源管理器停在别的项目时，卡片目标文件可能回退显示「当前文档」。phase6 P3-6 修复后的小残留，纯观感。
9. **可达性与主题细节一组**：ThemePicker 有 role=menu/menuitemradio 但无方向键导航、打开后焦点不进入菜单（`ThemePicker.vue:49-69`）；工作记录条目用 `<li @click>` 打开文档，键盘不可达（`AgentPanel.vue:572-578`）；`ThemeDefinition.dark` 字段声明后生产代码零使用（`theme.ts:8`）；CSP `script-src 'self'` 下无法内联预热脚本，已存深色主题时首帧闪默认浅色（`main.ts:7` 已尽力，属已知取舍）。
10. **watchTask 退避复位缺总次数兜底**（`web/src/api/client.ts:182`）：onopen 即重置退避，若服务端开连后立即断开且无终态事件，会以 500ms 间隔无限重连；契约允许复位，建议补总次数或总时长上限。
11. **测试缺口一组**：CancelledError→interrupted 分支无直接测试（`test_api_projects.py:451-457` 整体替换了 chat_project，未经过 recorder）；无并发对账测试；无字符串形态脱敏测试（见 P1-1）；无迁移重跑测试；无跨助手持 change_set id 访问应得 404 的断言；无「reject 一个 hunk 后接受同组其余 hunk」回归；work_item 断线补发无专项测试。
12. **phase6 两个维持暂缓项无 backlog 归口**（`docs/guides/backlog.md`）：第二轮 `tool_choice=none` 与温度硬编码（0.3/0.2）仅存在于 phase6 报告的遗留项表，backlog 观察项与 AGENTS.md 已知暂缓项均未提及，新会话只读 backlog 会漏掉。顺手补两条即可。

---

## 阶段 6 遗留项核对

| phase6 问题 | 当前状态 | 证据 |
|---|---|---|
| P2-1 保留窗口无总量兜底 | 已修复（有残留，见本报告 P3-1） | `_fit_messages_to_budget`（`context.py:90-136`）覆盖无可压缩前史/压缩失败/压缩成功三条超预算路径；`test_chat_context.py:214-275` 固化 8 条满额超长、单条 100k、压缩路径兜底；`token_budget=0` 按设计关闭兜底 |
| P3-1 压缩 info/warning Web 不可见 | 已解决（经 v1.19 工作记录） | warnings 经 `recorder.note` 进工作记录（`runtime.py:325-327`），AgentPanel 渲染 kind=warning；backlog 已归口。裸 info/warning 事件仍无 AgentPanel 分支，属设计内 |
| P3-2 助手 id 前后端口径 | 已修复 | `AssistantDialog.vue:17` 与 `assistant_registry.py:76` 同规则，错误文案一致；`AssistantDialog.test.ts` 3 例固化 |
| P3-3 LOCAL_PROXY 空值 warning | 已修复 | `mcp_client/registry.py:28-31` 降 debug，测试断言空 warning；附带观察（mcp_servers.json 绝对路径）已在 `README.md:31` 注明「重建后需同步」 |
| P3-4 中文标点未计入 CJK | 未修复，已登记暂缓 | `context.py:12` 正则未变；backlog 观察项 |
| P3-5 选区工具栏不随滚动 | 未修复，已登记暂缓 | `DocumentEditor.vue:153-162、182-185`；backlog 观察项 |
| P3-6 pending 作用域错位 | 已修复（有小残留，见本报告 P3-8） | `App.vue:43、55-57` 按 agentProjectId 过滤，`:120-133` 资源管理器切换不再清空；`App.test.ts:260-301` 固化 |
| P3-7 default 助手不预先禁用 | 未修复，维持合规暂缓 | `App.vue:418-423` 仍仅按剩余数量禁用；backlog 观察项 |
| P3-8 broker 队列无上界 | 未修复，已登记暂缓 | `api/tasks.py:125` 无 maxsize；backlog 观察项 |
| P3-9 README 链接过时 | 已修复 | `README.md:129-131` 改指架构 v1.22 与审查记录索引；phase6 报告已登记 `docs/README.md:35` |
| P3-10 Runtime assert 收窄 | 已修复 | `runtime.py:329-334` 显式 `raise RuntimeError`；agent/ 全包无以语句形式使用的 assert |
| 阶段 5：tool_choice / 温度硬编码 | 维持暂缓（缺 backlog 归口，见本报告 P3-12） | `runtime.py:496-501` 第二轮不携带 tools；`llm.py:126` 0.3 / `runtime.py:240` 0.2 未配置化 |

---

## 验证记录

- 本次为纯静态走读 + 二次核验（用户明确要求不重跑测试）：五路并行读完全区间 50 个文件的改动与关键文件现状全文；随后对全部 P1/P2 证据逐条复核——`agent/work_log.py` 全文、`agent/runtime.py:380-546`、`agent/context.py` 全文、`memory/projects.py` 迁移/reject/accept/finalize 各段、`ChangeDiff.vue` 全文、`AgentPanel.vue` 工作记录生命周期、`styles.css` 语法色段落，并直接核对 `node_modules/@codemirror/language` 中 `defaultHighlightStyle` 的定义方式（`dist/index.js:1803` 起，内联样式 spec 而非 class 映射）。
- 契约比对：架构 v1.22 的 v1.19–v1.22 各节（§3.3 兜底、§4.7/§5.7/§5.9 hunk 模型、§5.4 工作记录、§5.10 双视图、§9 风险表）与实现抽查一致；唯一例外是 §5.10 侧栏卡片导航未实现（本报告 P2-5）。
- 测试基线账面核对（读测试不跑测试）：`git grep -c "def test_"` 计 208 个函数，加 4 处 parametrize 展开 11 例恰为 219，与 AGENTS.md 声明精确吻合；前端 11 个 `*.test.ts` 中 `it/test` 恰 97 处。
- 敏感信息核对：全区间 diff 无真实密钥；`.env.example` 仅占位值；`config/mcp_servers.json` 走 `${TAVILY_API_KEY}` 插值；`git check-ignore` 确认 `.env`、`data/` 被忽略；`git ls-files` 无数据库、日志、构建产物入库。
- 工作树状态：审查开始时 `git status` 干净，本报告针对已提交代码。

## 处理建议

按用户惯例，本次审查不改动任何代码。建议顺序：

1. **P1-1（脱敏绕过）最优先**：改动极小（先 `json.loads` 再 redact），但必须先补字符串形态的 RED 用例再修；在接入任何新工具（尤其带凭据的 MCP server）之前必须完成。
2. **P1-2（语法高亮主题化）**：属 v1.22 契约补齐，需要一次自定义 HighlightStyle 的小重构，顺带验证五套主题的实际呈现。
3. P2-1 至 P2-3 为后端健壮性项，改动都很小，可合并一次提交按 RED → GREEN 处理；P2-4、P2-5 是用户可见项，可与前端改动一并安排。
4. P3 按精力择机：P3-3（文案）、P3-12（backlog 登记）是顺手项；P3-11 的测试缺口建议随对应修复逐项补。

---

## 处理结果（2026-08-18，v1.23）

本节记录审查后的处理闭环；上文发现、行号与 `219/97` 测试数保留为 2026-08-17 审查时点的历史事实，不回写覆盖。

- **P1 已闭环**：字符串形态的工具参数/结果先解析 JSON 再递归脱敏；CodeMirror 改用显式语义 class 的 `tagHighlighter`，五套主题均覆盖实际语法 token。用户确认脱敏验收边界为不得暴露完整密钥；允许展示不足以还原密钥的局部前缀，不再作为缺陷处理。
- **P2 已闭环**：reject 前恢复孤儿写意图；工作明细落库失败降级为 warning 且不打断任务；失败详情增加值级脱敏与 2,000 字符正文上限；连续对话归档上一轮工作记录；侧栏卡片与具体 hunk 均可打开目标文档，具体 hunk 会在编辑器中选中并滚动定位。
- **随修复完成的 P3/复核项**：accept-all 中断文案已修正；phase6 的 `tool_choice` 与温度配置观察项已登记 backlog；明细落库失败产生的 warning 复用失败明细序号，不占用 `event_seq=200`；主题高亮导出的每个语义 class 均有显式 CSS 规则。
- **继续暂缓**：未改变行为的低风险 P3 已统一登记到 `docs/guides/backlog.md`，由后续独立阶段按需处理。

处理过程遵循 RED → GREEN：Python 针对性测试 `19 passed`，记忆隔离红线 `10 passed`；最终完整基线为 Python `225/225`、前端 `102/102`，`npm run typecheck` 与 `npm run build` 均通过，`git diff --check` 无格式错误。

本报告已登记到 `docs/README.md`；架构单一事实来源已更新为 v1.23，README、AGENTS.md 与新会话提示词的当前基线已同步。
