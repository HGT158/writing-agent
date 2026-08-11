# 写作 IDE、项目导入与 AI 修改设计

日期：2026-08-10

本设计对应架构 v1.9 的阶段 4。若本文件与
`docs/phase1-architecture.md` 冲突，以架构文档为准。

## 产品模型

Web UI 是一个面向写作的轻量 IDE，而不是以聊天记录为中心的应用。

- 一个 Assistant 是一个隔离的写作工作空间。
- 一个 Assistant 可以拥有多个文章项目。
- 一个文章项目对应助手目录下的一个受管文件夹。
- 项目文件是正文和素材的事实来源；数据库只保存身份、版本、任务和修改建议元数据。
- Agent 聊天绑定当前项目，是操作项目内容的入口之一。

受管目录固定为：

```text
data/assistants/<assistant_id>/projects/<project_id>/
```

项目显示名与 `project_id` 分离，因此允许同名导入，但绝不覆盖已有项目目录。
除导入外，用户也可以新建空白项目（默认含 `article.md`）、重命名显示名和归档
项目。归档移动整个项目目录；只有显式 purge 才物理清除。运行中或存在 pending
change set 的项目拒绝归档。

## 导入

所有导入都复制内容，不引用外部路径，也不与外部文件双向同步。

### 文件夹导入

一个导入文件夹创建一个文章项目。浏览器目录选择器提交文件和合法相对路径，
API 在临时项目目录中重建完整目录树；全部文件写入成功后，再把临时目录原子
改名为正式项目目录。

目录导入保留嵌套结构。绝对路径、`..`、空路径段、符号链接、Windows 重解析点
以及解析后越过项目根目录的目标一律拒绝。导入失败时删除本次临时目录，不留下
可见的半成品项目。

单次导入默认上限为 5000 个文件、总计 512 MiB、单文件 100 MiB。实现通过设置
对象提供可收紧的配置，但不能关闭路径校验。请求超限时整次导入失败。

### 文本文件导入

单个 `.md`、`.markdown` 或 `.txt` 文件可直接导入。系统按源文件名创建一个
项目文件夹，将原文件复制进项目并作为首次打开的文档。源文件没有扩展名或不在
上述文本类型中时拒绝按“文本文件”导入。

文件夹项目中，`.md`、`.markdown`、`.txt` 可编辑；其他普通文件可以显示在
资源管理器中，但阶段 4 不承诺编辑。导入完成后，外部原文件的后续变化不会同步。

## 项目与文档数据

SQLite 新增按 `assistant_id` 隔离的项目元数据：

- `projects`：`project_id`、`assistant_id`、显示名、受管根目录、创建时间。
- `project_documents`：`document_id`、`assistant_id`、`project_id`、相对路径、
  `document_version`、更新时间。
- `change_sets`：`change_set_id`、助手/项目/文档归属、来源模式、基准版本、
  原文范围、原文快照、替换文本、状态和关联任务。

文件内容不重复存入 SQLite。每个可编辑文件拥有单调递增的
`document_version`，用于手工保存和 AI 修改的乐观并发校验。本阶段不实现完整
版本历史。

上述表和受管目录只能通过 MemoryStore 访问。`api/`、`agent/` 与前端均不得
直接执行 SQL；任何查询都不能只凭 `project_id` 或 `document_id`，必须同时带
`assistant_id`。

## IDE 界面

界面采用安静、紧凑的 VS Code 式工作区：

```text
┌ 助手选择 / 当前项目 / 保存状态 ─────────────────────────────┐
│ 活动栏 │ 项目资源管理器 │ 多标签编辑器 / Markdown 预览 │ Agent │
│        │ 导入文件/文件夹│                                  │ 面板  │
│        │ 文件树         │                                  │ 聊天  │
└────────────────────────────────────────────────────────────┘
```

左侧资源管理器只显示当前助手的项目。打开文件时在中间创建编辑标签；多个项目文件
可以同时打开。切换助手时，前端必须关闭或重新加载不属于新助手的标签、项目树和
Agent 会话，不能保留可操作的跨助手状态。

中间编辑器使用 CodeMirror 6，支持 Markdown 和纯文本、未保存状态、保存冲突提示、
多标签及 Markdown 预览。右侧 Agent 面板绑定当前项目，默认以当前打开文件作为
上下文；其他文件只有在用户显式附加后才进入上下文。

## 选区局部改写

用户选择连续文本后，选区附近出现悬浮工具栏。点击工具栏输入框不能丢失
CodeMirror 内保存的选区。请求包含 `assistant_id`、`project_id`、`document_id`、
选区范围、选中文本、指令和 `document_version`。

选区范围使用半开区间 `[from, to)`，单位统一为 Unicode code point。CodeMirror
提供的 JavaScript UTF-16 偏移必须在发送前转换；后端切片后再与 `selected_text`
核对。这是包含中文、emoji 或组合字符时仍能精确应用修改的硬性契约。

Runtime 使用当前助手 persona 与 editing Skill 生成 change set。前端展示原文和
建议文本的 diff，提供接受、拒绝和重新生成。生成建议不修改文件；接受时再次验证：

- change set、项目和文档属于请求助手；
- change set 仍为 pending；
- 当前版本等于基准版本和请求版本；
- 当前范围文本仍等于原文快照。

验证通过后使用临时文件和原子替换更新正文，版本号加一并将 change set 标记为
applied。任一验证失败返回 409，文件保持不变。

文件更新使用“旧内容快照 → 临时文件 → 原子替换 → SQLite 元数据提交”的顺序。
如果元数据提交失败，服务端必须用旧内容快照原子恢复文件；不能出现正文已变化而
`document_version` 未变化的状态。

## Agent 聊天修改

右侧聊天绑定 `assistant_id` 与 `project_id`，可接收三种上下文：当前文件、当前
选区、用户显式附加的其他项目文件。用户可以要求解释、续写、审校或修改。

纯回答通过 token SSE 返回；产生文件修改时，Runtime 必须输出一个或多个
change set，由前端逐项展示 diff。聊天没有直接覆盖文件的权限。未选择项目时可做
普通问答，但不能执行项目文件修改。

选区改写与聊天共用 AgentRuntime、EventBus、AssistantRegistry、MemoryStore、
Skill/MCP 工具表和 `run_locks`。同一助手已有任务时返回既有 409；不同助手可并行。

## API 与事件

阶段 4 新增以下主要接口：

- `GET /api/projects?assistant_id=X`
- `POST /api/projects`
- `PATCH / DELETE /api/projects/{project_id}`
- `POST /api/projects/import-file`
- `POST /api/projects/import-folder`
- `GET /api/projects/{project_id}/tree?assistant_id=X`
- `GET/PUT /api/projects/{project_id}/documents/{document_id}`
- `POST /api/projects/{project_id}/documents/{document_id}/selection-rewrites`
- `POST /api/projects/{project_id}/change-sets/{change_set_id}/apply`
- `POST /api/projects/{project_id}/change-sets/{change_set_id}/reject`
- `POST /api/projects/{project_id}/agent/messages`

长任务继续使用 `GET /api/tasks/{task_id}/stream`。AI 提出修改时发送：

```json
{
  "type": "change_preview",
  "data": {
    "change_set_id": "...",
    "project_id": "...",
    "document_id": "...",
    "range": {"from": 10, "to": 24},
    "replacement": "建议替换文本",
    "document_version": 7
  }
}
```

## 验收标准

- 同一助手可导入并管理多个文件夹型文章项目。
- 可新建空白项目、重命名和归档；默认归档不物理删除项目内容。
- 导入文件夹保留目录树；导入文本文件自动创建独立项目文件夹。
- 导入内容是副本，外部源变化不影响受管项目；同名导入不覆盖。
- 项目、文件、Agent 上下文和 change set 全部按 `assistant_id` 隔离。
- 多标签编辑、手工保存、Markdown 预览和版本冲突提示可用。
- 选区悬浮工具栏在输入指令时保留选区，并能预览、接受、拒绝、重新生成。
- Agent 聊天能使用当前项目上下文；任何修改都先显示 diff，再由用户确认应用。
- 路径穿越、符号链接/重解析点和越界访问被拒绝。
- 完成态 `articles` API 只读；所有继续编辑统一转为项目文档，避免双写入口。
- 记忆隔离红线与全量测试在新增覆盖后保持通过。
