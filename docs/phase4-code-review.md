# 阶段 4 代码审查报告

> 审查对象：`writing-agent/` 阶段 4（FastAPI + SSE + Vue 3 写作 IDE）全部新增/改动源码：`api/`（main.py / tasks.py / models.py）、`memory/projects.py`、`agent/project_editing.py`、`agent/runtime.py`、`agent/tools.py`、`web/src/` 全部 .ts/.vue，以及阶段 2/3 已修复项的回归确认
> 审查日期：2026-08-10
> 审查方式：逐文件走读 + 边界输入动态复现（独立临时 data 目录，未改动任何项目代码）+ `pytest tests/` 与 `vitest run` / `vue-tsc` 实测
> 环境：Windows 11 / Python 3.13.14（conda `writing-agent`）/ pytest 9.1.1 / FastAPI 0.141.1 / Node 22.16.0 / marked 15.0.4 / Vue 3.5.13

---

## 总体评价

先说做得好的部分：

- **SQL 边界与助手隔离执行到位**：`api/`、`agent/`、`scheduler/` 无任何裸 SQL；`memory/projects.py` 全部参数化绑定且每条查询/更新都带 `assistant_id` 过滤；跨助手读文档/文章实测 404，已有测试固化（`test_api_projects.py`）。
- **路径安全体系化且实测有效**：`_safe_relative_path`（`memory/projects.py:122-135`）拒绝 `../`、绝对路径、盘符、Windows 保留名；`_project_root` resolve 后做父目录包含校验。动态复现中 `../evil.md`、`..\evil.md`、`C:/evil.md`、`notes/../../evil.md` 全部 400，无逃逸；静态托管实测 `..%2f` 系列穿越全部 404，`data/`、`.env`、源码不可达。
- **接受链路双重校验，绝不静默错写**：`apply_change_set`（`projects.py:520-567`）同时要求状态为 pending、`current.version == expected_version == base_version`、逐字比对原文快照，条件 UPDATE 用 `rowcount != 1` 兜底；重复接受被明确拒绝（实测 `RuntimeError: change set 已处理`）。快照与磁盘不一致时以磁盘现状为准拒绝应用。
- **导入原子落地且有限额**：staging 目录 + `os.replace` 原子提交，失败完整回滚清理；文件数/总量/单文件三重上限（`projects.py:374-383`）。
- **前端乐观并发与偏移换算正确**：保存/接受均携带 `document_version`，冲突统一 409；`unicodeOffsets.ts` 的 UTF-16 ↔ code point 双向换算对 emoji 代理对和中文均正确且有测试，后端还有选区文本交叉校验兜底（`runtime.py:151`）。
- **无 CORS 中间件是本地单用户应用的正确默认**；异常映射统一（`_raise_http`），未捕获异常走 FastAPI 默认 500，不泄漏 traceback。
- **回归无退化**：阶段 3 全部修复在位，红线 `test_memory_isolation.py` 扩至 7 例。

本阶段发现 **2 个 P0**（存储型 XSS；前端接受修改后编辑器不同步导致静默覆盖稿件）、**11 个 P1**、**22 个 P2**，均附复现证据或代码依据。问题主要集中在三类：前端的接受/保存/SSE 生命周期路径、后端"文件写入先于事务提交"的顺序缺陷、长驻服务（TaskBroker/SSE）的资源生命周期。

测试现状：`pytest tests/` → **86 passed**（5.22s）；前端 `vitest run` → **9 passed**；`vue-tsc --noEmit` 干净。

---

## P0 — 阻断验收

### 1. Markdown 预览：`v-html` 渲染未消毒的 marked 输出（存储型 XSS，可升级为对本地 API 的任意调用）

**位置**：`web/src/components/MarkdownPreview.vue:6,9`；`web/index.html`（无 CSP）；`api/main.py:282-284`（同源托管）

预览组件直接 `v-html` 注入 `marked.parse` 结果：

```vue
const html = computed(() => marked.parse(props.content, { breaks: true, gfm: true }) as string)
<template><article class="markdown-preview" v-html="html" /></template>
```

marked v15 **不做任何消毒**（sanitize 选项早已移除），原始 HTML 直通。文档内容来自用户导入的外部文件（`import-file`/`import-folder` 完全可能导入来自网络/他人的 .md）以及 AI 落盘的改写结果，属于不可信输入。`package.json` 中无任何消毒库，`index.html` 无 CSP meta。

**危害放大**：生产模式下前端由 FastAPI 同源托管，`<img src=x onerror=...>`、`<svg onload=...>` 类 payload 一旦执行，脚本与 `/api` 同源，可直接调用 `DELETE /api/assistants/{id}?purge=true`、覆写文档等接口，造成数据损毁。（`<script>` 经 innerHTML 不执行，但事件处理器型 payload 会执行。）

**复现证据**：导入一篇内容为 `![](x onerror=alert(1))` 变体或 `<img src=x onerror="fetch('/api/...')">` 的 .md 并点开预览即触发；全文唯一 `v-html` 就在这一处。

**修复建议**：引入 DOMPurify，`marked.parse` 输出后 `DOMPurify.sanitize`，禁 `on*` 属性与 `javascript:` 协议；`index.html` 增加 CSP（`default-src 'self'` 起步）。补一条"恶意 Markdown 不执行脚本"的前端测试。

### 2. 从 Agent 面板接受修改后编辑器内容不同步，用户下一次键入会静默覆盖刚接受的修改（数据损坏）

**位置**：`web/src/App.vue:96-103,143-153`；`web/src/components/DocumentEditor.vue:52-68,130-135`；`web/src/components/AgentPanel.vue:78`

同一个 chat change 在两处渲染"接受"按钮：AgentPanel 内（`AgentPanel.vue:78`）与编辑器内（`DocumentEditor.vue:144`，经 `externalChange` prop）。若用户点 **AgentPanel 侧**的接受：

- `App.applyAgentChange`（`App.vue:98-101`）调 API 成功后只执行 `workspace.replaceTab`（更新 store）；
- 但 CodeMirror 实例**不会同步**：`DocumentEditor` 只 watch `props.tab.document_id`（`:130-135`），编辑器内容仅在 `createEditor`（`:56` `doc: props.tab.content`）时注入一次，此后不存在 store→编辑器的内容通道，`App.vue:143-153` 也未给 `DocumentEditor` 绑 `:key` 强制重建。

结果：服务端已是新内容，编辑器仍显示旧内容。用户敲任何一个字符，`updateListener`（`:60-61`）把"旧内容+新字符"写回 store 并置 dirty，下次保存就把刚接受的 AI 修改**静默冲掉**。对比编辑器侧自己的接受路径（`DocumentEditor.vue:108-110`）会 `dispatch` 全文替换，是正确的——两条接受路径行为不一致。

**修复建议**：统一接受入口（建议 AgentPanel 只展示，接受动作收敛到编辑器侧）；或在 `DocumentEditor` 增加对 `tab.content`/`tab.version` 的 watcher，外部替换且编辑器内容不一致时 dispatch 同步；或给 `DocumentEditor` 绑定重建键。补"接受后编辑器内容与 store 一致"的组件测试。

---

## P1 — 应修复

### 后端

#### 1. `save_document` / `apply_change_set` 先写文件、后提交事务：跨进程竞态下"冲突方恢复"会销毁已提交内容

**位置**：`memory/projects.py:425-452`（save）、`:538-563`（apply 同构）

顺序为「版本检查 → 写临时文件 → `os.replace` 覆盖正文 → 条件 UPDATE → commit」。单进程内被 `MemoryStore._lock`（`store.py:85`，threading.Lock 覆盖整个调用）串行化掩盖；但架构明确支持跨进程（run_locks 即为此设计，CLI/`agent schedule`/API 三个入口可同时运行），而 save/apply **不走 run_locks**。多进程下两个写者都通过版本检查后先后 `os.replace`，后者的条件 UPDATE 命中 0 行，回滚分支用**自己读到的旧字节**恢复文件——此时文件里已是先写者的新内容：

```python
os.replace(temporary, path)          # L435 文件已变成先写者的新内容
...
if cursor.rowcount != 1:
    raise RuntimeError("版本冲突")
except Exception:
    conn.rollback()
    restore.write_bytes(old_bytes)   # L448-450 用旧字节覆盖 → 抹掉已提交内容
    os.replace(restore, path)
```

**复现证据**（两条连接模拟两进程，在 A 的 UPDATE 前放行 B 完整保存）：

```
[B] 保存成功: version=2, content='B的内容'
[A] 保存结果: 抛出 版本冲突
[终态] DB version=2, DB 读到的 content=''
[终态] 磁盘文件实际内容=''
==> DB 认为已是 v2，磁盘却退回 v1 空内容；已提交内容彻底丢失
```

另注意：崩溃落在 L435~L445 之间同样造成"文件已新、DB 仍旧版"分叉，且 `save_document` 无任何内容校验能发现它。

**修复建议**：倒置顺序——先 `BEGIN IMMEDIATE` + 条件 UPDATE（版本校验在 DB 原子完成），成功后再写文件并 commit；写文件失败则 rollback。冲突方根本不会触碰文件，"恢复"逻辑可整体删除。此项与 P1-2 共用同一修复。

#### 2. apply 进行中被硬杀：留下"文件已改 + change set 永久 pending"的不可恢复孤儿，且无限阻塞归档

**位置**：`memory/projects.py:538-563`（先 `os.replace` L542，后 `BEGIN IMMEDIATE` L544）

进程在文件替换之后、事务提交之前被杀（kill/OOM/断电），WAL 恢复会回滚未提交事务，但磁盘文件不会回滚：文件已含新内容、版本未增、change set 仍 `pending`。由于内容已变，快照校验使**重试 apply 永远失败**；pending 又使归档被无限阻塞（`projects.py:271-277`）。唯一出路是手工 reject——但 reject 后磁盘（已应用）与 DB（未应用）分叉，无任何对账机制。

**复现证据**（子进程在 `conn.commit()` 瞬间自杀）：

```
[终态] 磁盘文件='这是改写文本。'（已被 replace）
[终态] DB version=2, change set status=pending
[重试 apply]  失败: RuntimeError: 原文快照不匹配
[尝试归档]    被阻: RuntimeError: 项目存在待处理 change set，拒绝归档
```

**修复建议**：P1-1 的"事务先行"方案可彻底关闭此窗口；另建议启动时对账，或对"pending 但快照已不匹配"的 change set 提供显式清理/强制 reject 出口。

#### 3. `chat_project` 多 change set 逐个提交：中途失败留下孤儿 pending，无回滚、用户不可见

**位置**：`agent/runtime.py:256-271`（循环内逐个 `create_change_set`，每个独立 commit）；`api/main.py:223-241`（无 change set 列表端点）

LLM 返回 N 个 changes 时逐个落库；第 k 个失败（文档不存在、版本过期、范围非法）抛异常后，前 k-1 个已提交的 pending change set 没有回滚。API 层没有任何列出 change set 的端点，任务失败时用户只拿到 error 文本，孤儿 pending 无法被 UI 发现，而孤儿会**永久阻塞该项目归档**。

**复现证据**（FakeLLM 返回 2 个 changes，第 2 个 document_id 不存在）：

```
[chat_project] 抛出: KeyError: '文档不存在：missingdoc123456'
[孤儿] 已提交的 change set 6a85146da8d449b9: status=pending
[归档该项目] 被阻: RuntimeError: 项目存在待处理 change set，拒绝归档
```

**修复建议**：先对全部 changes 做 dry-run 校验（文档存在、版本、范围、快照），再在单个事务内批量插入；或失败时显式回滚已创建的 change set；补 `GET /api/projects/{id}/change-sets?status=pending` 列表端点。

#### 4. TaskBroker 任务记录只增不减，长期运行内存无限增长

**位置**：`api/tasks.py:27,30-32,40-43`

`records` 字典在 `start()` 时插入，全文件没有任何删除/淘汰路径。每条 `TaskRecord` 永久保留完整事件历史 `events` 和完整 `result`——`POST /api/tasks` 的 result 是整个 `AgentState`（含 `draft` 全文、`memory_context`、`observations`，见 `agent/schemas.py:65-82` 与 `main.py:92-94`）。无 SSE 消费者时 `record.queue` 还会把全部事件再复制一份。

**复现证据**：`20 个任务完成后 records 总数=20；无消费者时单任务 events=51 条、queue 积压=51 条（双份内存）`。Scheduler 长驻场景下这是随任务数线性增长的常驻内存。

**修复建议**：任务进入终态且无活动流后删除 record（或清空 `events`/`queue` 只留摘要），或加 TTL/LRU 上限；`result` 只存必要字段。

#### 5. `POST /api/tasks` 无前置校验：不存在的助手/助手正忙一律 202，失败文本还泄漏全部助手 id

**位置**：`api/main.py:90-96`；`agent/runtime.py:72-80`

同步端点对 KeyError→404、`AssistantBusyError`→409（`main.py:29-33`），但异步任务端点把同样的错误推迟成 `status=failed`，客户端无法在请求时得到 404/409，与"一助手一任务"的拒绝语义脱节；且 `KeyError` 消息含可用助手列表，原样进入 `GET /api/tasks/{id}` 的 `error` 字段。

**复现证据**：未知助手 `HTTP 202` → `error="'助手不存在：ghost。可用助手：default'"`；预占锁后 `HTTP 202` → `error='助手 default 正忙：任务 seed-task-001 运行中'`。

**修复建议**：端点内同步执行 `runtime.assistants.get(body.assistant_id)`（→404）与 `runtime.store.is_locked(...)` 预检（→409）再入队；`record.error` 输出前剥离助手列表。

#### 6. 内置工具沙箱未排除项目目录：LLM 可绕过 change set 机制直写项目文件

**位置**：`agent/tools.py:20-26,56-60`

`save_markdown` 接受任意 `data/` 内相对路径，而受管项目目录位于 `data/assistants/<id>/projects/` 之内——即 LLM 在普通任务中可以直接 `save_markdown(path="assistants/default/projects/<pid>/article.md")` 覆写项目文档，**完全绕过 change set 与版本机制**（版本不增、无预览、无确认）。触发途径不必是恶意：导入的外部素材本身可携带提示注入文本，诱导模型调用该路径。阶段 4 的"AI 修改只产生 change set"约定在工具层没有强制。

**修复建议**：`_safe_resolve` 对写操作排除 `assistants/*/projects/` 前缀（读可保留），或将项目目录移出工具可达沙箱根；补一条红线测试。

### 前端

#### 7. 保存/改写/接受使用 `activeProjectId` 而非标签页自身的 `project_id`：跨项目打开的标签保存必失败

**位置**：`web/src/App.vue:85-87,99,146`；`web/src/components/DocumentEditor.vue:81-88,108`

多标签可跨项目打开（`workspace.openDocument` 只按 documentId 去重），但 `activeProjectId` 只反映资源树当前选中的项目。打开项目 A 的文档后点击项目 B，再保存 A 的标签：请求打到 `PUT /api/projects/B/documents/docA` → 404，内容保存失败。选区改写（`DocumentEditor.vue:88`）、接受修改（`:108`、`App.vue:99`）同样用错项目 ID——`ChangePreview` 里明明带 `project_id` 字段却没用。

**修复建议**：保存用 `tab.project_id`；change set 操作用 `change.project_id`。

#### 8. 接受修改会无提示丢弃当前未保存的编辑

**位置**：`web/src/App.vue:101`；`web/src/components/DocumentEditor.vue:109`

两条接受路径都用服务端返回的完整文档**整体替换**编辑器/store 内容。若用户有 dirty 编辑（此时发送的 `document_version` 仍是上次保存的版本），这些键入内容被无声丢弃，无确认、无合并。两处均未检查 `tab.dirty`。

**修复建议**：接受前若 dirty 则弹确认（"接受将丢弃未保存修改"），或先自动保存再接受。

#### 9. SSE 生命周期缺陷：EventSource 不保存、不关闭、无 onerror → loading/sending 永久卡死，断线重连事件重复

**位置**：`web/src/api/client.ts:68-76`；`web/src/components/DocumentEditor.vue:89-96`；`web/src/components/AgentPanel.vue:31-46`

三个问题叠加：

1. `watchTask` 返回的 `EventSource` 在两个调用方都被直接丢弃，组件卸载/切换文档时无法关闭，回调继续操作已卸载组件的状态；
2. 没有 `source.onerror`：SSE 建连失败或服务器重启（task 记录丢失返回 404，EventSource 无限重试）时，`DocumentEditor` 的 `loading`（仅在 `task_done` 置 false）和 `AgentPanel` 的 `sending`（仅在终态置 false）**永久为 true**，改写工具栏/发送框被永久禁用；
3. 后端 `stream()` 每次连接从 `index = 0` 全量重放（`tasks.py:68-73`），且无 `id:`/Last-Event-ID。浏览器断线自动重连后 `token` 事件再次 push，AgentPanel 出现重复消息气泡，`change_preview` 重复触发。

**修复建议**：`watchTask` 增加 `onerror` 回调；调用方保存引用并在 `onBeforeUnmount`/文档切换时 `close()`；后端事件加自增 `id:` 或前端按序号去重。

#### 10. `applyAgentChange` / `rejectAgentChange` / `DocumentEditor.rejectChange` 无 try/catch，错误静默吞掉

**位置**：`web/src/App.vue:96-103,105-109`；`web/src/components/DocumentEditor.vue:120-126`

后端对"change set 已处理""版本冲突"返回 409。这些函数未捕获异常：用户看不到任何提示（Unhandled Promise Rejection），且 `externalChange.value = null` 不会执行，预览卡在原处，反复点反复失败。典型触发路径：同一 change 在 AgentPanel 与编辑器两处都显示接受按钮（见 P0-2），先点一处后点另一处必触发"已处理"。对比同文件 `saveActive`（`App.vue:90-93`）是有 catch 的。

**修复建议**：包 try/catch，错误写入 `globalError`/`error`；"已处理"类错误直接清空预览即可。

#### 11. 关闭脏标签页/关闭页面均无确认，未保存稿件直接丢失

**位置**：`web/src/stores/workspace.ts:57-64`；`web/src/components/EditorTabs.vue:13`；全应用无 `beforeunload`

对写作工具而言这是高危缺口：`closeTab` 不检查 `tab.dirty` 直接 `splice`；浏览器标签关闭/刷新也没有拦截；无自动保存。误点一次关闭按钮，未保存内容不可恢复。

**修复建议**：关闭脏标签前确认（或自动保存）；注册 `beforeunload`；可考虑防抖自动保存。

---

## P2 — 建议修复

### 后端

**1. 多客户端订阅同一任务最多延迟 15 秒**（`api/tasks.py:66-79`）：所有流式客户端共用同一个 `record.queue`，`put_nowait` 只唤醒一个 getter，其他客户端要等 15s 超时 keepalive 后才回到 `record.events` 重放。复现实测两客户端收到同一事件的耗时为 0.5s vs 15.0s。建议按客户端注册独立队列或广播。

**2. 任务被取消后状态永远停在 running**（`api/tasks.py:45-57,81-86`）：`runner` 只捕获 `Exception`，接不住 `CancelledError`（BaseException）；`shutdown()` 主动 cancel 时 `record.status` 保持 `"running"`，不产生终态事件，`stream()` 永不收尾。另外目前没有任何取消/停止任务的 API。建议补 `except asyncio.CancelledError` 写终态；如需对外停止能力补 `DELETE /api/tasks/{id}`。

**3. 任务端点无助手维度**（`api/main.py:257-280`）：`GET /api/tasks/{id}` 与 `/stream` 不校验 `assistant_id`，结果含他助手的 `memory_context`、完整草稿与 change 原文。虽为本地单用户且 task_id 为 16 位十六进制，但与隔离红线精神不一致。建议 `TaskRecord` 记录 `assistant_id` 并在端点校验，或在文档中把 task_id 明确定义为能力令牌。

**4. async 端点内直接执行同步阻塞 IO**（`api/main.py:99-206` 等全部直调 MemoryStore 的端点）：`MemoryStore` 全部是同步 sqlite3 + 同步文件 IO，在 `async def` 里直接执行；导入上限 512MB 期间事件循环完全阻塞（SSE keepalive 与其他请求全部停摆）。建议重 IO 端点改 `def`（FastAPI 自动丢线程池）或 `asyncio.to_thread` 包装。

**5. 输入大小无上限**（`api/models.py:12-15,28-31`）：导入有三重限额，但 `DocumentSave.content`、`AgentTaskRequest.task` 无 `max_length`，也无全局请求体限制。建议加合理上限。

**6. 错误码语义不一致**（`api/main.py:98-112`）：列表接口对不存在的助手静默 200 空数组（写路径会先 `assistants.get` 得 404）；`project_id` 非法时读路径 404、写路径 400。建议统一助手存在性校验与 id 格式错误码。另：`_raise_http` 靠错误文本中的中文词（"冲突""已处理""运行中"等，`main.py:34-37`）匹配 409，属脆弱的字符串契约，建议改为专用异常类型。

**7. 助手 purge 用 LIKE 清理 checkpoints，`_` 是通配符**（`memory/store.py:402`）：助手 id 允许 `_`（`assistant_registry.py:75`），purge `my_bot` 会命中 `myXbot:...` 的 thread_id。窗口极窄但触碰隔离红线。建议 `LIKE ? ESCAPE '\'` 转义，或改精确前缀比较。

**8. 模块级 `app = create_app()` 在 import 时即打开真实 data/ 数据库并构造 Runtime**（`api/main.py:288`）：任何 `import api.main`（含测试）都会加载真实配置与数据库，不跑 lifespan 则资源永不 close。建议只保留工厂，uvicorn 入口用 `create_app` 或单独的 `api.server` 模块。

**9. archive/purge 与运行锁的 check-then-act 竞态**（`memory/store.py:201-217`、`memory/projects.py:244-265`）：忙检查通过后归档并不持锁，另一进程恰好启动的编辑任务提交的 change set 会成为孤儿（复现：`[孤儿] status=pending` → apply 报"项目不存在"）；反向问题——检查不做 TTL+PID 回收，崩溃残留的过期锁会让归档/清除一直报"任务运行中"，而 `acquire_lock` 明明能回收同一把锁（复现：过期 5h+死 PID 的锁行阻塞归档）。建议让 archive/purge 复用 `acquire_lock`/`release_lock` 语义，把忙检查与变更放进同一临界区。

**10. 助手 purge：store 层不设防 + `data/articles/` 残留**（`memory/store.py:391-407`、`agent/assistant_registry.py:104-119`）：`store.purge_assistant` 本身不检查 run_locks，直接删除全部 SQL 行（含锁行本身）；registry 层的 `is_locked` 前置检查与 purge 非原子。`purge=True` 清理了助手目录与 `archive/projects/<id>`，却漏掉 `data/articles/<id>/`——复现实测 purge 后残留文章文件，且持锁期间 purge 成功、锁行被连带删除。建议 purge 入口复用带回收语义的忙检查，文件级联补 `data/articles/<id>`。

**11. 导入路径校验边缘遗漏**（`memory/projects.py:122-135`）：无穿越问题，但 Windows 非法字符 `<>:"|?*` 与单段 >255 长度未校验——复现 `'a<b.txt'` 通过校验后落盘 OSError → HTTP 500（应为 400）；保留名检测不一致（`con .txt` 放行而 `con.txt` 拒绝）；未做 NFC 归一化；无导入深度上限。建议在 `_safe_relative_path` 内补黑名单、长度上限、`stem.rstrip(" .")` 后判保留名、`unicodedata.normalize("NFC")`。

**12. `create_change_set` 不校验 original_text**（`memory/projects.py:455-487`）：chat 链路完全信任 LLM 输出的快照，坏快照能以 pending 入库，直到用户点接受才报"原文快照不匹配"，期间同样阻塞归档（与 P1-3 叠加）。现有测试明确允许此行为（安全达标），属体验改进：建议创建时即比对快照，不一致快速失败。

**13. 崩溃残骸无恢复；归档项目无法 purge**（`memory/projects.py:252-264,286-306,369-391`）：归档半程（目录已 move、DB 未提交）项目变砖；`.purge-*`/`.import-*` staging 无人清扫；**已归档项目没有任何清除路径**——`purge_project` 经 `_project_row` 要求 `archived_at IS NULL`，对归档项目 DELETE?purge=true 得 404，只能等整个助手 purge。建议启动时清扫孤儿 staging，支持对已归档项目的 purge。

**14. 编码与偏移量契约**（`memory/projects.py:331,434,541`；`agent/runtime.py:149-152`）：GBK/Big5 的 `.txt` 导入后 `get_document` 抛 `UnicodeDecodeError`（→400），该文档永久不可读不可保存，导入时无拦截；读用 `utf-8-sig`、写用 `utf-8`，带 BOM 文件首次保存后 BOM 被无声剥离；偏移量单位是 Python code point 但 API/Schema 未声明契约，前端若换算错误会被守卫拦下（安全但难诊断）。建议导入时探测可解码性、统一 BOM 策略、在 API 文档中明确偏移单位。

**15. 其他低危项**：`projects.py:534` 生产路径使用 `assert`（`python -O` 下失效）；`change_sets.status/source` 无 CHECK 约束；`acquire_lock` 用 `psutil.pid_exists` 判活，PID 复用时残留锁被误判存活而永久拒收（`store.py:357-358`）；`archive_project` 归档目录时间戳用本地时间而其余字段均 UTC。

### 前端

**16. `codePointToUtf16Offset` 死代码**（`web/src/components/DocumentEditor.vue:10`）：反向换算只在测试里出现；后端返回的 `range`（码点偏移）没有映射回编辑器做高亮。建议用反向换算高亮选区，或删除未用导入；另对代理对中间的选区边界显式取整到字符边界。

**17. SSE 事件数据零校验**（`DocumentEditor.vue:91`、`AgentPanel.vue:34`、`client.ts:71`）：`as unknown as ChangePreview` 强转不做形状校验，字段变更后 `change_set_id` 为 undefined 会拼进 URL 才暴露；`JSON.parse(message.data)` 抛错成未捕获异常，EventSource 保持打开而 loading 卡死。建议写运行时校验函数并包 try/catch。

**18. 保存按钮无进行中防抖**（`App.vue:79-94,126`）：请求在途时仍可再次点击，两次 PUT 携带相同版本号，第二次必 409"版本冲突"——实际已保存成功，用户却看到误导性错误。建议加 `saving` 标志。

**19. 多处异步入口缺错误处理，且在 await 前修改状态**（`App.vue:29-45,125`、`workspace.ts:20-27`）：`switchAssistant`/`selectProject`/`openDocument` 无 try/catch，失败即 Unhandled Rejection；`selectProject` 先置位再请求，失败后 UI 停留在"已选中但树为空"；`workspace.switchAssistant` 先清标签再 await 项目列表，列表失败时旧标签已丢。建议统一 catch + 状态后置。

**20. 切换文档时本地状态重置不完整，旧任务预览可能注入新文档**（`DocumentEditor.vue:130-135`）：切文档清了 `toolbar`/`localChange`，但没清 `error`/`showPreview`；旧文档发起的选区改写任务 EventSource 未关闭（见 P1-9），任务随后完成时 `change_preview` 会把旧文档的修改塞进新文档界面，接受会打错目标。建议回调内校验 `document_id` 一致再采纳。

**21. AgentPanel 切换项目/助手不清会话与待审修改；多 change 只保留最后一个**（`AgentPanel.vue:11-13`）：`messages`/`activeChange` 不随 props 变化清理，切项目后旧 change 仍可点接受（叠加 P1-7 会拼错项目 URL）；后端 chat 一次可返回多个 changes（`runtime.py:258` 逐个 emit），前端 `activeChange` 只留最后一个，其余 pending 滞留数据库且无 UI 处理。建议 watch props 清空会话；`activeChange` 改列表。

**22. 版本冲突后无恢复路径**（`App.vue:90-93`、`workspace.ts:50-55`）：保存遇到冲突只显示错误，用户除手动刷新无计可施；`updateActiveContent` 无论内容是否真的变化都置 dirty。建议冲突时提供"覆盖我的/加载服务器版"二选一；内容未变不置脏。

### 前后端契约软性不一致

- `ChangeSetAction.document_version` 在 `models.py:45` 定义为可选，但 apply 端点强制必填否则 422（`main.py:225-226`）——模型与端点语义不一致，前端恰好总传值才没踩坑。
- apply 响应含 `change_set` 字段（`main.py:232`），前端类型只声明 `{ document }`（`client.ts:55`），多余字段被忽略。
- 后端还会发 `thought`/`tool_call`/`tool_result`/`warning`/`info`/`failed` 事件（`agent/events.py:58-89`），前端只消费 4 种，其余静默丢弃；其中运行时 `failed` 与 broker `task_failed` 重复表达同一失败，前端只认后者。
- `client.ts:25-29,67` 的 `renameProject`/`archiveProject`/`getTask` 定义但全项目无调用方（死代码）。
- `marked.parse(...) as string`（`MarkdownPreview.vue:6`）：v15 返回类型是 `string | Promise<string>`，当前同步选项下运行时安全，但属强转掩盖类型。

未发现端点不存在或字段拼写不一致的硬性错误，前后端契约整体对齐良好。

---

## 测试覆盖缺口

**后端**（现有 86 例覆盖：助手管理、mock runtime 任务链路、项目 CRUD、文档保存+版本冲突+跨助手 404、导入正常路径、purge、待处理 change set 阻止归档、选区改写 SSE 完成后重放+apply、项目聊天）：

1. 零并发测试：两个连接/进程对同一文档并发 `save_document`（P1-1）、save 与 apply 交叉竞态无用例。
2. 零崩溃注入测试：apply/save 中途 kill 后的终态与恢复（P1-2）、启动对 `.purge-*`/`.import-*`/归档半程的清扫。
3. chat 多 change 部分失败：无"第二个 change 非法时第一个应回滚/上报"用例；无 pending change set 列表端点可测。
4. archive/purge × run_locks：无 TOCTOU 窗口用例；无"过期+死 PID 残留锁不应阻塞归档"用例（当前实现会阻塞）。
5. 助手 purge：无"持锁时拒绝"用例；无断言 `data/articles/<id>` 被删除的用例。
6. 导入路径边缘：Windows 非法字符、>255 长名、`con .txt` 变体、深度上限、NFC/NFD、大小写碰撞均无覆盖（现有用例仅覆盖 `../`、绝对路径、盘符）。
7. 状态机补全：重复 reject、apply 后 reject、reject 后再 apply、对已归档项目的操作均无用例（重复 apply 行为实测正确但未固化）。
8. API 语义：POST /api/tasks 未知助手/正忙的当前行为无测试；任务端点 404 无测试；导入限额/扩展名/paths-files 不一致无测试；静态托管穿越无回归测试。
9. 偏移量与编码：emoji 跨选区端到端用例、GBK 导入、BOM 保持均无覆盖。
10. 工具沙箱：无"save_markdown 不得写入 `assistants/*/projects/`"的红线测试（P1-6）。

**前端**（现有 9 例）：

1. `DocumentEditor.vue` 零测试——选区改写提交、接受/拒绝、接受后编辑器与 store 同步（P0-2 场景）、external/local change 优先级全部未覆盖，为最高风险缺口。
2. SSE 消费零测试——AgentPanel 测试 mock 掉了 `watchTask`，恰好绕开真实逻辑（P1-9）。
3. 保存冲突/并发路径零测试——409 分支、双连点、跨项目保存（P1-7）、apply 版本冲突。
4. `client.ts` 错误处理零测试——非 2xx detail 抽取、非 JSON 错误体、网络异常。
5. `MarkdownPreview` 零测试——应补恶意 Markdown 消毒断言（P0-1）。
6. `workspace.test.ts` 未覆盖 `closeTab` 激活回落与 `openDocument` 去重。
7. 反向偏移换算"测了死代码、活代码没测"的倒挂（P2-16）。

---

## 结论

阶段 4 的后端硬约定（SQL 边界、助手隔离、路径沙箱、乐观锁双闸门）执行质量延续阶段 3 水准，86/86 + 9/9 全绿、类型检查干净；但新引入的 Web 层把两个此前不存在的风险面带了进来：**浏览器侧的不可信内容渲染**（P0-1 XSS）与**多入口修改同一文档的状态同步**（P0-2 静默覆盖稿件），二者都属于"用户正常操作即可触发/导入外部文件即可携带"的现实风险，建议先修。

后端最系统的缺陷是"文件写入先于事务提交"的顺序问题（P1-1/P1-2 同根因，一次重构可同时关闭竞态与崩溃两个窗口），其次是多 change 原子性（P1-3）与 TaskBroker/SSE 的长驻资源生命周期（P1-4、P2-1/2）。前端则集中在接受/保存路径的项目 ID 取值、dirty 状态保护和 SSE 生命周期。

建议修复顺序：**P0 ×2 → 后端 P1-1/2（同修）→ P1-3 → 前端 P1-7~11 → P1-4/5/6 → P2 按模块批量处理**。修复完成后按惯例跑红线 + 全量回归，并在本文档追加"处理结果"对照表。

*复现脚本（repro_api/、repro_edit/ 下 r1~r7）保存于本次审查的工作区，如需重放验证可另行提供。*

---

## 处理结果（v1.10）

本轮按 TDD 逐项复现；确认存在的问题已修复并补回归测试。未列为“已修复”的项目是增强建议或需要单独产品决策，未将其伪装成已完成。

| 审查项 | 结论 | 处理 |
|---|---|---|
| P0-1 Markdown XSS | 存在 | `marked` 输出经 DOMPurify 消毒，增加 CSP 和恶意 Markdown 测试 |
| P0-2 AgentPanel 接受后编辑器不同步 | 存在 | 监听标签正文/版本变化同步 CodeMirror，抑制同步产生的 dirty 事件 |
| P1-1/P1-2 文件先写后事务、崩溃分叉 | 存在 | `document_write_intents` + `BEGIN IMMEDIATE` + 原子替换；下次 MemoryStore 操作可恢复意图，冲突方不再恢复旧文件 |
| P1-3 chat 多 change 逐个提交 | 存在 | `create_change_sets` 全量校验后单事务批量创建，任一项失败整批回滚 |
| P1-4 TaskBroker 无界增长 | 存在 | 终态记录和事件均有容量上限，空闲终态记录淘汰 |
| P1-5 POST /api/tasks 无前置校验 | 存在 | 入队前校验助手存在和运行锁，未知助手 404、正忙 409 |
| P1-6 内置工具可直写项目 | 存在 | `save_markdown` 拒绝 `assistants/*/projects/*` 受管路径 |
| P1-7 跨项目使用 activeProjectId | 存在 | 保存、应用、拒绝均使用标签或 change 自身 `project_id`；标签按项目+文档复合身份 |
| P1-8 dirty 接受覆盖 | 存在 | 接受 AI 修改前显式确认丢弃未保存编辑 |
| P1-9 SSE 生命周期 | 存在 | API 按助手加 query；组件保存并关闭 EventSource，解析/网络错误结束 loading |
| P1-10 异步 apply/reject 无错误处理 | 存在 | App、DocumentEditor、AgentPanel 增加错误捕获和可见错误状态 |
| P1-11 dirty 标签/离页无保护 | 存在 | 关闭标签、切换助手、beforeunload 增加确认/浏览器保护 |
| P2-1/P2-2 SSE 广播与取消 | 存在 | 每个订阅者独立队列；CancelledError 进入 task_failed 终态 |
| P2-3 任务端点无助手维度 | 存在 | TaskRecord 绑定 `assistant_id`，状态和 stream 跨助手按 404 处理 |
| P2-5 输入无上限 | 存在 | 任务 100,000 字、文档保存 2,000,000 字上限 |
| P2-7 checkpoint LIKE `_` 通配 | 存在 | 使用 `LIKE ... ESCAPE` 并转义前缀 |
| P2-8 import api.main 副作用 | 存在 | `api.main` 只保留工厂，Uvicorn 入口迁至 `api.server:app` |
| P2-9 archive/purge TOCTOU 与死锁残留 | 存在 | 项目/助手归档清除复用跨进程 mutation lock；`is_locked` 回收过期死亡锁 |
| P2-10 助手 purge 残留文章/绕过锁 | 存在 | purge 检查运行锁并清理 `data/articles/<assistant_id>` |
| P2-11 导入 Windows 非法路径/编码 | 存在 | NFC、非法字符、长度/深度和 UTF-8 导入校验 |
| P2-12 change set 延迟校验快照 | 存在 | 创建时即校验 `original_text` |
| P2-13 归档项目无法 purge | 存在 | `purge_project` 支持已归档项目 |
| P2-16/P2-17 前端死代码与事件形状 | 存在 | 删除未用偏移转换导入，补 TaskEvent/ChangePreview 运行时校验 |
| P2-18 保存重复点击 | 存在 | 增加 saving 防抖 |
| P2-19/P2-20 异步入口与旧任务注入 | 存在 | 入口错误捕获；切换文档/项目关闭旧流并校验 change 归属 |
| P2-21 AgentPanel 会话与多 change | 存在 | 切换范围清理会话；待审 change 改为列表显示 |
| P2-4 同步 IO、P2-6 专用异常、P2-14 BOM/偏移文档、P2-22 冲突恢复 UI | 部分为增强项 | 当前实现未改变既有契约，列入后续独立改进，不影响本轮已确认缺陷修复 |

回归结果：Python **106/106**，`tests/test_memory_isolation.py` **9/9**，前端 **14/14**，`vue-tsc` 和 Vite 生产构建通过；`agent/`、`scheduler/`、`api/` SQL 边界扫描为空。

---

## 复审核验结果（2026-08-10）

> 复审方式：对 v1.10 处理结果逐项走读核验；关键项用独立临时 data 目录做跨进程/杀进程动态复现（脚本位于本次审查工作区 verify_backend/ v1~v7）；未改动任何项目代码。
> 复测基线：`pytest tests/` → **106 passed**（6.47s）；红线 `test_memory_isolation.py` → **9 passed**；前端 `vitest run` → **14 passed**（8 个测试文件）；`vue-tsc --noEmit` 干净；`web/dist` 已按修复后源码重建（含 CSP meta 与 DOMPurify 调用标记）。

### 一、已确认修复（附核验证据）

| 审查项 | 核验证据 |
|---|---|
| P0-1 Markdown XSS | `MarkdownPreview.vue:7-10` marked 输出经 `DOMPurify.sanitize`（默认配置，未放宽）；`web/index.html:6` 增加 CSP；`MarkdownPreview.test.ts` 断言 `onerror` 剥离、`javascript:` 拦截；`dist/index.html` 同步含 CSP |
| P0-2 接受后编辑器不同步 | `DocumentEditor.vue:169-178` 新增 content/version watcher，不一致时 dispatch 全文替换；`syncingExternalContent` 标志抑制同步产生的 update（不误置 dirty）；`DocumentEditor.test.ts` 断言外部替换后编辑器显示新内容 |
| P1-1/P1-2 事务先行 | `projects.py` `_save_document_impl`/apply 改为 `BEGIN IMMEDIATE` → 版本校验 → INSERT `document_write_intents` → commit → 才 `_write_atomic`；旧"旧字节恢复"分支已删除；`_recover_write_intents_locked` 在后续操作时恢复意图。**动态复现**：两进程竞态保存，冲突方报"文档正在被其他进程写入"且文件未变，终态 DB/磁盘一致无分叉；apply 提交前杀进程后，一次 `get_document` 即完成恢复（升版 + pending→applied + 意图清零），归档不再被永久阻塞 |
| P1-3 多 change 原子性 | `runtime.py:257-273` 改单次 `create_change_sets`；`projects.py:710-768` 单事务内全量校验（含快照比对）后批量插入，任一失败整批回滚；`test_runtime_project_editing.py` 断言第 2 个 change 非法时 pending 数为 0 |
| P1-4 TaskBroker 无界增长 | `api/tasks.py` `max_records=128, max_events=512`，`_trim_records` 在 start/终态/流关闭时淘汰终态且无订阅者的记录。动态复现：12 任务后 records 降至上限内，事件历史被截断 |
| P1-5 任务端点预检 | `main.py:91-97` 入队前 `assistants.get`（404）+ `is_locked`（409）。动态复现：未知助手 → 404 且不再泄漏助手列表 |
| P1-6 工具沙箱排除项目目录 | `tools.py:29-37` `_reject_managed_project_write` 拒绝 `assistants/*/projects/*` 写路径；`test_tool_registry.py` 覆盖 |
| P1-7 项目 ID 取值 | 保存用 `tab.project_id`、接受/拒绝用 `change.project_id`（`App.vue:111-134,144`、`DocumentEditor.vue:131,147`）；标签改项目+文档复合键；`workspace.test.ts` 覆盖跨项目同名文档 |
| P1-8 dirty 接受丢弃 | 两条接受路径均在 dirty 时弹确认（`App.vue:130`、`DocumentEditor.vue:128`） |
| P1-9 SSE 生命周期 | `client.ts:77-99` watchTask 返回句柄 + `onError` 回调，onerror 即 close；两组件保存句柄并在卸载/切文档/切范围时关闭、复位 loading/sending；任务端点按 `assistant_id` 校验。浏览器自动重连已被 onerror-close 消除，重复事件路径不复存在 |
| P1-10 apply/reject 错误处理 | App/DocumentEditor/AgentPanel 各异步入口均有 catch 并写入可见错误 |
| P1-11 脏标签/离页保护 | 关闭脏标签、切换助手前确认（`App.vue:37,151-155`）+ `beforeunload`（`App.vue:157-161`） |
| P2-1 多客户端广播 | 每订阅者独立队列（`tasks.py:22,101-102`）。动态复现：两订阅者收到同一事件延迟均 0.0s |
| P2-2 CancelledError 终态 | `tasks.py:74-81` 捕获 CancelledError → status=failed + `task_failed` 事件；`test_task_broker.py` 覆盖 cancel 与 shutdown 两路径 |
| P2-3 任务端点助手维度 | `TaskRecord.assistant_id` + `get()` 跨助手 404；状态/stream 端点强制 `assistant_id` query |
| P2-5 输入上限 | `models.py` task 100,000 字、content 2,000,000 字。动态复现：2,000,001 字保存 → 422 |
| P2-7 LIKE `_` 通配 | `store.py:450-456` `LIKE ? ESCAPE '\'` + 转义；红线测试含 `my_bot` 不误删 `myXbot:` 用例 |
| P2-8 import 副作用 | `api/main.py` 只留工厂，入口迁 `api/server.py:4`；README 同步 `uvicorn api.server:app` |
| P2-9 archive/purge TOCTOU | archive/purge 先 `acquire_lock`（跨进程 mutation lock）再执行；`_live_lock_locked` 回收过期+死 PID 锁。动态复现：5h 死锁残留下归档成功，活跃锁下归档 → 409 |
| P2-10 助手 purge | purge 前忙检查（豁免自身 mutation 锁）+ `rmtree(data/articles/<id>)`；registry delete 全程持锁。动态复现：持锁 purge → AssistantBusyError；purge 后 articles 无残留 |
| P2-11（主体） | NFC 归一化、非法字符、单段 >255、深度 >64、NUL、尾随空格/点均拒绝（动态复现 `a<b.txt` → 400）；GBK 导入拦截见 P2-14 |
| P2-12 快照前置校验 | `projects.py:744-745` 创建时即比对 `content[start:end] != original_text` |
| P2-13（部分） | 已归档项目可 purge（`_project_row_any`），purge 同步清理 write intents。动态复现通过 |
| P2-14（部分） | GBK/非 UTF-8 可编辑文件导入被拒（`projects.py:569-573`），不再"变砖" |
| P2-15① / P2-16 / P2-17 / P2-18 / P2-19 / P2-20 / P2-21 / marked 强转 | 生产路径 assert 已移除；未用偏移导入已删；TaskEvent/ChangePreview 运行时校验（`client.ts:18-25`、`types.ts:54-66`）；saving 防抖；异步入口 catch + 状态后置；切文档关流清状态 + change 归属校验；AgentPanel 切范围清会话 + 待审 change 改列表；marked 返回类型 typeof 收窄 |

### 二、部分修复 / 残留子项（本轮复核实测）

1. **P2-11 残留：`con .txt`、`nul .md` 等保留名变体仍被放行**。`projects.py:162` `part.rstrip(" .").split(".", 1)[0]` 顺序错误——rstrip 先作用于整个文件名（`con .txt` 不以空格/点结尾，不剥离），split 得到 `"CON "` 不在保留集。应为 `part.split(".", 1)[0].rstrip(" .")` 后再判断。动态实测：`'con .txt' → 放行`、`'nul .md' → 放行`（`con.txt`/`NUL.md` 正确拒绝）。旧版 Windows 上该名字解析为设备名。
2. **P1-5 同类残留：两个异步编辑端点无预检**。`main.py:227` selection-rewrites 与 `main.py:259` agent/messages 均未做 `assistants.get`/`is_locked` 前置校验；动态实测未知助手 → 202 后 task_failed，`error` 字段仍含完整助手列表（`'助手不存在：ghost。可用助手：default'`）。/api/tasks 上关闭的泄漏面在这两个端点保留。
3. **P2-13 残留：崩溃残骸无启动清扫**。全代码无 `.purge-*`/`.import-*`/归档半程的对账清扫（grep 确认）；purge 在 `os.replace→commit` 之间、archive 在 `shutil.move→commit` 之间崩溃仍会使项目变砖且无恢复路径（write intents 机制只覆盖文档保存/apply，未覆盖项目级操作）。
4. **P2-14 残留**：BOM 仍静默剥离（读 `utf-8-sig`/写 `utf-8`，动态实测保存后磁盘 BOM 消失）；偏移量单位（code point）仍未在 API/Schema 声明。与 v1.10 "列入后续"声明一致。
5. **P2-15 残留**：`change_sets.status/source` 仍无 CHECK 约束；`psutil.pid_exists` 判活的 PID 复用误判仍在（`store.py:388,426`），且新增一处于写入意图存活判定（`projects.py:302`，见 R1）；归档目录时间戳仍用本地时间（`projects.py:438`）。
6. **P2-16 残留**：组件导入已删，但 `utils/unicodeOffsets.ts:6-10` 的 `codePointToUtf16Offset` 在生产代码中仍无调用方（只有测试引用），"测了死代码"的倒挂未消除；range 回映高亮未实现。
7. **契约软性项未处理**（与 v1.10 声明一致，均为低危）：`ChangeSetAction.document_version` 模型可选但 apply 端点手工 422 必填（`models.py:45`、`main.py:240-241`）；apply 响应含 `change_set` 字段而前端类型只声明 `document`（`main.py:247`、`client.ts:64`）；`client.ts:34-38,76` 的 `renameProject`/`archiveProject`/`getTask` 仍是无调用方的死代码。
8. **已声明暂缓项确认**：P2-4（async 端点同步阻塞 IO）、P2-6 后半（`_raise_http` 中文字符串匹配 409）、P2-22（版本冲突恢复 UI、`updateActiveContent` 无条件置 dirty）与 v1.10 声明一致，本轮未改，不计为未修复。

### 三、本轮修复引入的新问题

**R1（P2，最值得跟进）｜写入意图无 TTL + PID 复用误判，可能造成文档级永久阻塞**
`projects.py:302`（存活判定）、`:75` 附近表结构无 TTL 字段。`document_write_intents` 的 owner 判活用 `psutil.pid_exists` 且无 TTL：持意图进程死亡后若 PID 被系统复用，该文档后续一切读写永久抛"文档正在被其他进程写入"，无任何自愈或强制清理出口（对比 run_locks 至少有 TTL+回收）。建议给意图行加 `acquired_at` + TTL，或复用 run_locks 的回收语义。

**R2（P2）｜新错误文本与 409 字符串契约错位**
跨进程忙写入抛 `RuntimeError("文档正在被其他进程写入")`，不含 `_raise_http`（`main.py:34-37`）的任一关键词（冲突/已处理/待处理/运行中），实测映射为 HTTP 400 而非 409，客户端难以与参数错误区分。这正是原 P2-6 指摘的字符串契约脆弱性的具体兑现，建议一并改专用异常类型。

**R3（P2）｜恢复逻辑在写事务内做磁盘 IO**
`_recover_write_intents_locked`（`projects.py:284-345`）在 `BEGIN IMMEDIATE` 写事务内执行 `_write_atomic`，且 `get_document` 等纯读路径也可能升级为写事务；跨进程竞争下若持锁超过 sqlite 默认 5s busy timeout，`sqlite3.OperationalError` 不被 `_raise_http` 捕获 → HTTP 500。建议恢复路径限制事务内耗时，或将 busy_timeout 显式调高并在 `_raise_http` 中处理 OperationalError。

**R4（P3）｜写文件失败的补救事务语义意外**
`_write_atomic` 失败后删除意图的补救事务若也失败（如他进程持写锁），该"失败保存"的内容会在后续任意操作恢复时被真正写入并升版——调用方收到异常但内容最终生效。建议补救失败时记 warning 并保留意图让恢复走原意图路径，语义保持单一。

**R5（P2）｜AgentPanel 乐观移除待审卡片，apply 失败后无法重试**
`AgentPanel.vue:72-79` 在 emit apply/reject 之前就把 change 从 `activeChanges` 过滤掉。后端返回 409（版本冲突/已处理）时卡片已消失，用户只在 App 全局错误条看到信息，无法从面板重试该 change。建议失败时恢复卡片或保留至结果确认。

**R6（P3）｜切文档窄竞态残留**
`DocumentEditor.submitSelection` 中若 `rewriteSelection` POST 在途时切换文档，watch 的 `stopStream()` 先于新流赋值执行，旧任务的流会挂到新文档界面；回调写入 `localChange` 前不校验归属（`currentChange()` 对 localChange 不做归属校验）。因 apply 使用 `change.project_id` + 版本校验不会写错文档，属 UI 混淆而非数据损坏，窗口很窄。`AgentPanel.send` 在途窗口同构。建议回调内校验 `document_id` 归属。

**R7（P3）｜杂项**：保存按钮 `:disabled` 未绑定 `saving`（逻辑防抖生效但视觉上可连点，`App.vue:180`）；`workspace.switchAssistant` 仍在 await 前置 `assistantId`（`workspace.ts:20`），失败后短暂状态不一致；CSP `img-src 'self' data: https:` 允许预览加载任意外站图片（跟踪面，非 XSS，可后续收紧）；`DocumentEditor.vue:110` 类型守卫后仍保留冗余 `as unknown as` 强转；`_WRITE_GUARDS` 按文档只增不减（增长极慢，可忽略）；`save_markdown` 仍可直写 `assistants/<id>/memory/profile.md` 覆盖长期画像（沙箱写排除只覆盖了 projects/，相邻观察项，非本轮修复引入）。

### 四、新增测试覆盖评估

新增 5 个测试文件/扩展（前端 9→14 例、Python 86→106 例），覆盖了原缺口清单中最关键的几项：SSE client 层真实逻辑（client.test.ts）、恶意 Markdown 消毒（MarkdownPreview.test.ts）、P0-2 编辑器同步（DocumentEditor.test.ts）、跨项目同名标签（workspace.test.ts）、TaskBroker 淘汰与取消（test_task_broker.py）、写入意图竞态/恢复（test_project_store.py）、快照前置校验、purge 隔离（test_memory_isolation.py 扩至 9 例）。仍缺：DocumentEditor 选区改写提交/接受拒绝/dirty 确认分支、保存 409 与双连点、SSE 组件级真实流消费、`request<T>()` 非 2xx detail 抽取、closeTab 激活回落与 beforeunload。

### 五、结论

v1.10 处理结果**真实可信**：2 个 P0 与全部 11 个 P1 均经代码走读 + 动态复现双重确认修复，其中事务先行 + 写入意图机制确实消除了 DB/磁盘分叉与永久孤儿 pending 两类最严重缺陷；9/9 完全修复的 P2 亦有测试或复现证据。106/106、9/9、14/14 全绿，dist 已同步重建。

遗留事项按优先级：**R1（意图无 TTL + PID 复用）建议优先处理**，这是新机制引入的唯一可能造成永久阻塞的路径；其次是二处点名残留——`con .txt` 保留名变体放行（一行顺序修复）与两个异步编辑端点的预检缺失（复用 start_task 的预检即可）；R2/R3 与 P2-6 字符串契约建议合并为一次异常类型整改。其余 R 项与契约软性项均为 P3 级体验/卫生问题，可并入已声明的后续增强批次。

阶段 4 审查闭环：无阻断性问题，遗留项不改变验收结论。

---

## 第二轮复审处理结果（v1.11，2026-08-10）

复审核验列出的实际问题均已按架构 v1.11 修复；行为变更先补失败测试确认 RED，再实现 GREEN。未扩大阶段 4 产品范围。

| 复审项 | 处理结果 |
|---|---|
| P2-11 Windows 保留名变体 | 调整基础名归一化顺序，`con .txt`、`nul .md` 等变体与标准保留名一并拒绝。 |
| 两个异步编辑端点缺少预检 | 选区改写和项目聊天在返回 202 前校验助手存在及运行锁；未知助手 404，助手忙 409。 |
| P2-13 项目级崩溃残骸 | MemoryStore 启动时对账 `.import-*`、`.purge-*` 和唯一可归属的归档半程目录。 |
| P2-14 BOM 与偏移单位 | 导入时记录 UTF-8 BOM，保存、apply 和恢复均保持；OpenAPI 明确选区偏移为 Unicode code point。 |
| P2-15 数据与进程所有权 | `change_sets.source/status` 增加 CHECK 与迁移；运行锁和写入意图使用 PID + 进程启动时间，旧行以 TTL 兜底；归档时间戳改为 UTC。 |
| P2-16 偏移转换倒挂 | 后端 code-point 范围在生产路径转换为 CodeMirror 使用的 UTF-16 范围。 |
| 契约软性项 | apply 模型强制 `document_version`，reject 使用独立模型；前端响应类型补 `change_set`，删除无调用方客户端方法。 |
| R2/R3 冲突和长写事务 | 引入专用冲突异常并稳定映射 HTTP 409；恢复改为短事务认领、事务外文件 IO、短事务终结，SQLite busy timeout 显式设为 10 秒。 |
| R4 恢复失败语义 | 恢复文件写入失败时保留持久化意图并释放认领，下一次操作可重试；新增回归测试验证先失败后恢复成功。 |
| R5/R6 异步 UI 竞态 | 待审卡片仅在父级确认 apply/reject 成功后移除；助手/项目/文档范围变化后忽略旧 POST、错误和 SSE 事件。 |
| R7 与相邻卫生项 | 保存中禁用按钮；助手切换改为事务式提交；内容未变化不置 dirty；CSP 禁止任意 HTTPS 图片；去除冗余强转；写锁守卫改弱引用；`save_markdown` 禁止写入全部助手受管数据。 |
| 独立复核：旧运行锁 TTL | `pid_started_at=0` 的迁移旧行在 TTL 到期后直接回收，即使该 PID 已被其他存活进程复用也不会永久阻塞。 |
| 独立复核：工作区乱序响应 | 助手切换使用请求代次；文档打开、项目刷新和 App 项目树同时校验助手快照与代次，旧响应不再注入新工作区。 |
| 独立复核：apply/reject 越界 | DocumentEditor 的 apply/reject 捕获助手、项目、文档和代次；切换范围后的响应、错误和 finally 均不修改当前编辑器。 |
| 独立复核：同范围再次发送 | AgentPanel 发送/重新生成时保留既有 pending 卡片，只在范围切换或父级确认审核成功后清除。 |
| 独立复核：CHECK 迁移原子性 | `change_sets` 重命名、建表、复制、删旧表和建索引置于同一 `BEGIN IMMEDIATE`；非法旧值使迁移失败时完整回滚，旧表及数据保留供修复重试。 |
| 独立复核：写入意图插入竞态 | 恢复查询与意图登记之间若被另一进程抢先占位，唯一键异常转换为专用 `ResourceConflictError`，API 稳定返回 409。 |

补充自审发现并修复：恢复路径第一次文件系统写入失败时，原实现会删除已经持久化的恢复意图，导致无法再次恢复。现改为条件释放认领但保留意图，第二次读取可接管并完成正文写入、版本终结。

最终回归：Python **123/123**，`tests/test_memory_isolation.py` **9/9**，前端 **30/30**；`vue-tsc`、Vite 生产构建通过，`web/dist` 已重建；`agent/`、`scheduler/`、`api/` 无 `.execute` / `.executemany` / `.executescript` SQL 调用。

阶段 4 v1.11 复审修复闭环完成，按阶段门停止，不自动进入下一阶段。
