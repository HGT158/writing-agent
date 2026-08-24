# 阶段 8 代码审查报告

> 审查对象：`writing-agent/` 阶段 7 审查基线（`f80031d`，v1.22 文档同步）之后的全部已提交改动，即 v1.23 phase7 复审加固，共 1 个提交（`6021520`），21 个文件、+728/−58 行
> 审查日期：2026-08-21
> 审查方式：全量 diff 静态走读 + 改动文件现状全文复读 + 关键缺陷用 conda 环境只读片段复现与修复验证。按用户要求，本次未改动任何代码，也未重跑 pytest/vitest（测试已由用户跑过并全绿）
> 环境：Windows 11 / 与 AGENTS.md 声明环境一致

---

## 总体评价

本次提交是 phase7 报告（2 个 P1、5 个 P2）的处理闭环。先说结论：**七项闭环里六项半属实且质量不错，但修复代码自身引入了一个新的 P1**——失败详情值级脱敏的替换公式算错了切片边界，在最常见的触发形态下会原样泄漏完整密钥。

做得好的部分：

- **P1-1（脱敏绕过）的修复机制正确**：`_redact_string` 对字符串先 `json.loads`、解析出对象/数组再递归 `redact`、解析失败或标量按原文回退（`agent/work_log.py:83-92`），生产路径 `args=call.arguments`、`result=output` 两条 JSON 字符串通道全部覆盖（`agent/runtime.py:411、435`）；配套「字符串形态脱敏」与「非 JSON 原文保留」两个用例（`tests/test_work_log.py:256-307`）。
- **P1-2（语法高亮主题化）修复属实且验证手段扎实**：`web/src/editor/themeHighlight.ts` 用 `tagHighlighter`（已核实 `@lezer/highlight` 确有导出）定义 18 个 tag → `cm-*` 语义类映射，经 `syntaxHighlighting` 引入且刻意不带 `fallback: true`（注释解释了与 basicSetup 内置 `defaultHighlightStyle` 合并的冲突机理）。逐一核对：18 个语义类在 `styles.css:319-328` 全部有显式规则，所用 `--cm-keyword/--cm-string/--cm-comment/--cm-heading/--cm-link/--editor-fg/--muted` 变量在五套主题块中全部定义；`tags.monospace`（行内代码）虽未被两者覆盖，但 `defaultHighlightStyle` 对其本就无样式定义，行内代码继承主题化的编辑器前景色，无硬编码色残留。两条测试分别断言 DOM 语义类出现与「每个导出类必有 CSS 规则」的契约。
- **P2-1（reject 孤儿写意图）修复属实**：`reject_change_hunk` 增加 `data_dir` 并在事务前 `_recover_write_intents`（`memory/projects.py:1521-1528`），与 accept/save/create/读文档路径一致；`memory/store.py:518-525` 门面同步传参，API 层签名不变；测试按「进程死在文件替换与 finalize 之间」重建孤儿意图并断言恢复语义（完成替换 + 元数据终结），另补「reject 一个 hunk 后接受同组其余 hunk」回归（phase7 P3-11 缺口之一）。
- **P2-2（明细落库失败打挂整轮）核心目标达成**：`done()` 的落库包进 try/except，失败降级为 warning 工作项并记日志，任务终态不再受影响（`agent/work_log.py:233-239、265-294`）；降级 warning 复用失败明细空出的序号、不占用 200 溢出位，有专门用例固化（`tests/test_work_log.py` 第 199 条失败场景）。残留见本报告 P2-1。
- **P2-4（上一轮工作记录消失）修复设计合理**：`archiveLiveWork` 在 `liveUserIndex` 切换到本轮之前把终态 `liveWork` 归档进 `workRecords`，为乐观消息分配稳定负数 id 保持交错视图轮次位置（`web/src/components/AgentPanel.vue:236-251、432-433`）。核对生命周期无重复/丢失：会话内负数 id 稳定、服务端消息 id 恒正不冲突；`loadSession`/`clearConversation` 整体替换为服务端数据，归档项被服务端等价物接管。测试断言两轮记录与消息的交错顺序。
- **P2-5（侧栏导航）链路完整**：`ChangeDiff.vue:16、25-29` 卡片头与 hunk 块 `role="button"` 可点击/回车，emit `open(hunkId?)` → `AgentPanel.vue:280-287` 转发 → `App.vue:136-148` `openDocument` 携带 `hunkId` → `DocumentEditor.vue:138-149` `focusHunk` 定位滚动。时序核对无竞态：`workspace.openDocument` 等内容加载完才建标签（`web/src/stores/workspace.ts:39-59`），`nextTick` 后编辑器与 `inlineDiffs` 均就绪。三处测试覆盖 emit、转发与 expose 接线。
- **P3 顺手项与文档同步齐全**：accept-all 中断文案修复（`App.vue:324-325`）；phase6 两个暂缓项（`tool_choice`、温度硬编码）与 phase7 全部 P3 观察项登记 backlog；架构文档升版 v1.23 且 §5.7 契约文本同步细化（截断标注不计入正文上限、降级复用序号、值级凭据形态清单），phase7 报告连同处理结果一节入库并登记 `docs/README.md`。
- **仓库卫生保持**：全区间 diff 无真实密钥（脱敏测试仅用 `sk-secret`/`sk-abcdef123456` 等合成值）；测试基线账面精确吻合——按 test 函数清点 214 个 + 4 处 parametrize 展开 11 例 = 225，前端 `it/test` 恰 102，与 AGENTS.md/README/new-session-prompt 三处声明一致。

本阶段发现 **0 个 P0、1 个 P1、1 个 P2、6 个 P3**。P1 是新引入的：值级脱敏替换公式的切片边界多算了一个 `match.start()`，导致「key=value」形态的敏感值在短前缀时泄漏前若干字符、在前缀长于值时（异常文本的常见形态）**完整泄漏并复制匹配后的文本**——恰好击穿本次修复自己写入架构的「完整匹配的敏感值不得落库」与用户确认的「不得暴露完整密钥」验收边界，且配套测试断言过弱未能拦住。

测试现状（引自 AGENTS.md 声明基线，用户已跑过，本次未重跑）：

```
Python 225/225 · 记忆隔离 10/10 · 前端 102/102 · vue-tsc 与生产构建通过
本次新增覆盖：test_work_log.py（+5 例）、test_change_hunks.py（+1 例、重写 1 例）、
前端 App/AgentPanel/DocumentEditor（+5 例）
```

---

## P0 — 阻断验收

未发现。

## P1 — 应修复

### 1. `_redact_secrets_in_text` 捕获组分支切片算错：key=value 形态敏感值部分或完整泄漏

**位置**：`agent/work_log.py:42-50`（缺陷行 `:48`），影响 `summarize_detail`（`:73-80`）即 `done`/`finish_task` 的失败详情落库与 SSE

替换函数对带捕获组的「key=value」分支意图是「保留键名前缀、只把值换成 `***`」，但切片终点写成 `match.start() + match.end() - len(value)`，正确值应为 `match.end() - len(value)`——多叠加了一个 `match.start()`。设匹配起点为 s、值长为 L：实际保留的前缀比预期多吃 s 个字符，即**泄漏敏感值的前 min(s, L) 个字符**；当 s > L（匹配位置之前有更长的文本，异常报文几乎总是如此）时**整个敏感值原样保留**，还会把匹配后的 s−L 个字符复制一份拼进结果。

已用 conda 环境只读片段复现（非跑测试）：

```
输入：'HTTP 401 Unauthorized from upstream server: invalid api_key=sk-abcdef123456 for request'
输出：'HTTP 401 ... invalid api_key=sk-abcdef123456 for request*** for request'
完整密钥泄漏：True
```

无捕获组的三个分支（`sk-`/`tvly-`/Bearer）整体替换，不受影响；缺陷只在键值分支。

**为什么测试没拦住**：`test_recorder_truncates_and_redacts_failure_detail` 的样例 detail 以「调用失败：」开头，s=5，只泄漏值的前 5 个字符（`sk-ab`），而断言只查「完整密钥串不在」与长度 ≤3000，恰好放行。弱断言 + 短前缀样例 = 错误信心，与 phase7 P1-1 的教训同型。

**修复建议**（已用只读片段验证正确）：切片改为 `match.string[match.start():match.end() - len(value)]`。补长前缀（s > 值长）场景用例，断言敏感值的**任意子串**（如 `"sk-abcdef" not in detail`）不出现、且匹配后文本不重复；同时把现有用例的断言从「全串不在」收紧为「敏感值前缀子串不在」。

## P2 — 建议修复

### 1. 明细落库失败的降级 warning 在实时视图不可见：只发 `work_item_done`，前端对未知 work_id 直接忽略

**位置**：`agent/work_log.py:282-290`（只 emit `work_item_done`，无配对 `work_item_start`），`web/src/components/AgentPanel.vue:193-226`（`work_item_done` 只更新经 start 建好的条目，`:216-225` 找不到即静默丢弃）

`_note_persist_failure` 为降级 warning 新建 work_id 后只发了 done 事件。前端 `handleWorkEvent` 的 done 分支按 work_id 在 `record.items` 里找既有项，找不到就什么也不做——于是这条「工作记录明细落库失败」warning 在实时会话里永远不会渲染；只有当降级条目自身落库成功、用户重载会话后才从持久化事件里浮现（若 DB 故障持续，降级条目也写不进，就只剩服务端日志）。phase7 P2-2 修复承诺的「补发一条 warning 工作项」用户实际看不到，补偿信号的实时性落空。现有测试只断言落库结果，未覆盖 SSE 可见性，故未暴露。

**修复建议**：补发配对的 `work_item_start`（复用同一 work_id；序号不变、仍走失败明细空出的槽），或前端对未知 work_id 的 done 事件就地补建条目；并补一条断言实时渲染的用例。

---

## P3 — 可优化

1. **`focusHunk` 静默无操作**（`web/src/components/DocumentEditor.vue:138-147`、`App.vue:136-148`）：标签页 dirty、hunk 已非 pending 或内容重定位失败时 `inlineDiffs` 里没有目标项，点击侧栏卡片只打开文档、不定居也不提示。建议找不到时回退到按 `hunk.original` 文本搜索定位，或给一条轻提示。
2. **`summarize_detail` 截断标注措辞**（`agent/work_log.py:79`）：「原始 N 字符」的 N 是脱敏**后**长度；发生脱敏时比真实原文短。建议改为「脱敏后 N 字符」或不标注口径。
3. **非 JSON 普通文本 args/result 不做值级扫描**（`agent/work_log.py:83-92`）：架构已明文「无法解析的普通文本保持原文」，属已接受边界，非缺陷；但值级扫描目前只作用于 detail，一旦接入返回纯文本且可能内嵌凭据的 MCP 工具，这里就是缺口。建议在 backlog 登记为观察项，在接入新工具前评估。
4. **弱断言测试一组**（`tests/test_work_log.py`）：除 P1 所述用例外，`test_recorder_detail_persists_store_failure_as_warning` 只断言 warning 存在与终态正确，未断言 SSE 序列（对应本报告 P2-1）；建议随修复一并收紧。
5. **ChangeDiff 键盘语义不全**（`web/src/components/ChangeDiff.vue:16、29`）：`role="button"` 只监听 Enter，未处理 Space（WAI-ARIA button 惯例两者都要）；与 phase7 P3-9 的无障碍暂缓项同族，可并入后续整理。
6. **import 分组小瑕疵**（`agent/work_log.py:17`）：`import logging` 孤立在 dataclass 导入之后，未与其他标准库导入归组；纯格式。

---

## phase7 发现项复核

| phase7 问题 | 当前状态 | 证据 |
|---|---|---|
| P1-1 脱敏被 JSON 字符串绕过 | 已修复 | `_redact_string`（`work_log.py:83-92`）+ `summarize_args/summarize_result` 字符串分支（`:95-120`）；`test_work_log.py:256-307` 两用例（字符串脱敏、非 JSON 回退） |
| P1-2 语法高亮未主题化 | 已修复 | `themeHighlight.ts` 18 个语义类映射 + `syntaxHighlighting`（非 fallback）；`styles.css:319-328` 全类有规则、五套主题变量齐备；DOM 类名测试 + CSS 契约测试（`DocumentEditor.test.ts:287-305`） |
| P2-1 reject 孤儿写意图持续 409 | 已修复 | `projects.py:1528` recover 前置；`test_change_hunks.py:353-385` 孤儿重建用例 |
| P2-2 明细落库失败打挂整轮 | 核心已修复，补发信号不可见（本报告 P2-1） | `work_log.py:233-239` try/except 降级；`test_work_log.py` 两例（普通降级、第 199 条降级不占 200） |
| P2-3 detail 脱敏与截断 | 截断已生效；值级脱敏公式错误（本报告 P1-1） | `summarize_detail` 接入 done/finish_task（`work_log.py:222、345`、`runtime.py:418、432、535`）；缺陷复现见 P1-1 |
| P2-4 上一轮记录随新轮消失 | 已修复 | `archiveLiveWork`（`AgentPanel.vue:236-251`）；`AgentPanel.test.ts:310-359` 两轮交错断言 |
| P2-5 侧栏卡片无导航 | 已修复 | `ChangeDiff.vue` open emit → `AgentPanel` 转发 → `App.openDocument(hunkId)` → `focusHunk`；三处测试覆盖 |
| P3-3 accept-all 文案残缺 | 已修复 | `App.vue:324-325` |
| P3-11 测试缺口（部分） | 字符串脱敏、同组 reject 后 accept 已覆盖 | 其余缺口仍登记于 backlog |
| P3-12 backlog 归口缺失 | 已修复 | `docs/guides/backlog.md` 补 `tool_choice` 与温度两条 |
| 其余 P3 观察项 | 维持暂缓，均已登记 backlog | `docs/guides/backlog.md`「暂不处理的观察项」 |

---

## 验证记录

- 本次为纯静态走读 + 二次核验（用户明确要求不重跑测试）：通读 `6021520` 全量 diff（1257 行）与全部改动文件现状全文——`agent/work_log.py`、`agent/runtime.py:380-546`、`memory/projects.py` accept/reject/recover 各段、`memory/store.py` 门面、`web/src` 的 App/AgentPanel/ChangeDiff/DocumentEditor/themeHighlight/workspace/styles.css，并核对 `web/node_modules` 中 `@lezer/highlight` 的 `tagHighlighter` 导出、`@lezer/markdown` 的 node→tag 映射与 `@codemirror/language` 的 `defaultHighlightStyle` 定义（确认无 monospace 规则，行内代码继承主题前景色）。
- 缺陷实证（只读，不改代码、不跑项目测试）：用 conda 环境直接调用 `summarize_detail` 复现 P1-1 两种泄漏形态，并用同一正则的修正表达式验证修复建议输出正确（`api_key=***`，无泄漏、无重复文本）。
- 测试基线账面核对（读测试不跑测试）：`def test_` 计 214 个函数，加 4 处 parametrize 展开 11 例恰为 225；前端 `*.test.ts` 中 `it/test` 恰 102，与 AGENTS.md/README/new-session-prompt 声明精确吻合。
- 契约比对：架构 v1.23 新增的 §5.7 工作记录加固条款（JSON 字符串先解析再脱敏、detail 2,000 字符与值级形态清单、降级复用序号不占 200、截断标注不计入正文上限）与实现逐条比对——除「完整匹配的敏感值不得落库」因 P1-1 未达成外，其余一致。
- 敏感信息核对：全区间 diff 无真实密钥，测试中的 `sk-`/`Bearer` 值均为合成夹具；无 `.env`、`data/`、构建产物入库。
- 工作树状态：审查开始时 `git status` 干净，本报告针对已提交代码；报告本身为唯一新增文件，未改动任何既有文件。

## 处理建议

按用户惯例，本次审查不改动任何代码。建议顺序：

1. **P1-1 最优先**：一行修复，补长前缀泄漏用例并收紧现有断言，再改切片表达式；在修复前，失败详情通道的值级脱敏应视为未生效。
2. **P2-1 随之**：补发配对 `work_item_start`（或前端兜底建项）+ 实时可见性用例，把降级补偿信号真正送达用户。
3. P3 按精力择机：P3-1（focusHunk 回退）与 P3-4（测试收紧）建议随上述修复一并处理，其余可登记 backlog。

---

## 处理结果（2026-08-22，v1.24）

本节记录审查后的处理闭环；上文发现、行号与 `225/102` 测试数保留为 2026-08-21 审查时点的历史事实，不回写覆盖。

- **P1-1 已闭环**：捕获组分支切片终点修正为 `match.end() - len(value)`，并按本报告建议补长前缀（s > 值长）用例、把既有 detail 脱敏断言从"完整密钥串不在"收紧为"敏感值任意前缀子串不得出现"；修复后另行用 conda 环境独立复现本报告的原始泄漏样例，确认 `api_key=***`、无子串泄漏、无尾部文本重复。
- **P2-1 已闭环**：采用服务端方案——`_note_persist_failure` 在 `work_item_done` 前补发配对的 `work_item_start`（同一 `work_id`、复用失败明细空出的序号、不占溢出位），前端既有 start 建项逻辑无需改动即渲染；用例断言 SSE start→done 配对序列（同时落实 P3-4 弱断言收紧）。
- **P3-1/P3-4 已随修复处理**：`focusHunk` 在内联装饰不可用时按 hunk 原文回退搜索定位并选中，找不到给轻提示；两条 vitest 用例分别覆盖回退选区与提示文案。
- **其余 P3 已登记 backlog**：截断标注口径（P3-2）、非 JSON 文本值级扫描（P3-3）、Space 键语义（P3-5）、import 分组（P3-6）见 `docs/guides/backlog.md` 观察项。
- **同版处理用户实测发现的四项缺陷**（见架构 v1.24 变更条目 (5)–(8)）：选区改写与项目聊天的 `change_preview` hunk 载荷补 `status` 字段（此前两条发射路径均缺，前端校验拒绝并报"无效的修改预览"）；运行中工作记录耗时改由每秒响应式时间源驱动自动跳动；外部内容同步保持滚动位置（文档身份 watcher 改多源按元素比较——数组 getter 恒触发曾致保存/接受 hunk 均销毁重建编辑器；内容同步改最小差异区间替换，不再整篇替换破坏滚动锚点）；失效建议可从侧栏放弃关闭（"全部放弃"此前只遍历 pending，全 stale 卡片点击为空操作导致永久滞留；全失效卡片隐藏"全部接受"）。

处理过程完成针对性验证与全量回归；最终完整基线为 Python `226/226`、记忆隔离红线 `10/10`、前端 `108/108`，`npm run typecheck` 与 `npm run build` 均通过。

本报告已登记到 `docs/README.md`；架构单一事实来源已更新为 v1.24，README、AGENTS.md 与新会话提示词的当前基线已同步。
