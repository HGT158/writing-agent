# 个人写作 Agent

面向内容生产的本地写作 Agent：网络检索、素材归纳、大纲规划、分段成文、质量检查和 Markdown 归档。
Planner 每轮动态选择 Skill 与工具，不是固定 Workflow。当前已完成阶段 4：FastAPI + SSE + Vue 3 写作工作台。

架构单一事实来源：[docs/architecture/phase1-architecture.md](docs/architecture/phase1-architecture.md)（v1.26；多主题界面、多 hunk 逐处审查、持久化工作记录、SSE 断线续传、上下文预算兜底、值级脱敏加固与树形资源管理器均已实现，v1.26 为文档口径对齐）。

## 核心能力

- 多助手及独立 persona、Skill、记忆和文章目录，可在界面上直接新建与归档助手。
- LangGraph 六节点 Agent Loop，支持 MCP、流式事件和跨任务记忆。
- 一助手多文章项目，支持新建或复制导入文本文件和文件夹；资源管理器为 VS Code 式树形（项目文件树紧贴项目行、子文件夹可展开收起、同级按名称交错排序），项目与文件行内重命名、删除（有确认；项目删除为归档语义、文件删除入口自动改指）。
- CodeMirror 多标签编辑、Markdown 预览和乐观版本保存。
- 选中文本后输入提示词局部改写，接受后才写入文件。
- AI 修改以 hunk 为最小审查单元：同文档多处建议一次生成，编辑器内联展示、每处独立接受/放弃（TRAE 式），侧栏卡片提供批量入口与按文档的状态对账。
- 右侧 Agent 面板支持流式项目聊天（Markdown 渲染）、每项目多会话历史和 change set 审核。
- 待确认修改采用双视图：编辑器内联展示删除/新增与接受/放弃控件，侧栏卡片汇总同一批建议，状态单一来源。
- 项目聊天按 token 预算分层压缩上下文：保留最近若干轮全文，更早历史压缩为可复用摘要。
- 项目聊天带可展开的持久化工作记录：实时显示进度、工具调用、警告与修改建议，终态自动折叠、刷新后可回看。
- 任务事件流断线后按游标自动续传（`after_seq` / `Last-Event-ID`），按 `seq` 去重；缺口可识别，聊天终态后自动从服务器恢复完整会话。
- 五套内置主题（纸墨/墨夜/暖卷/竹青/海湾）：未手动选择时实时跟随系统深浅偏好，仅用户显式选择后持久化；深色主题完整覆盖编辑器（含语法高亮）。
- 工作记录脱敏与降级：字符串参数与失败详情均做敏感字段/值级脱敏并截断，明细落库失败降级为实时可见的警告，不打断任务。
- 侧栏修改建议卡片可点击打开目标文档并在编辑器中定位该处修改；内联定位不可用时按原文回退搜索，仍找不到则提示。
- 接受修改或保存后编辑器保持滚动位置、选区与撤销历史（最小差异区间同步，不重建编辑器）；已失效的建议可从侧栏整卡放弃清理。
- APScheduler 定时任务与跨进程助手运行锁。

## 环境要求

- Windows 11
- Python 3.13，固定使用 `C:\miniconda\envs\writing-agent\python.exe`
- Node.js 20+
- 现代浏览器：Chrome/Edge 111+ 或 Firefox 113+（界面样式使用 `color-mix`）
- OpenAI 兼容 LLM API Key；Tavily API Key 可选
- `config/mcp_servers.json` 的 fetch 项硬编码了上述 conda 环境的 `python.exe` 绝对路径；重建或迁移环境后需同步修改

## 快速安装

```powershell
cd D:\test_agent\writing-agent

# 本机 conda 需要禁用插件并使用 classic solver
$env:CONDA_NO_PLUGINS = "true"
C:\miniconda\Scripts\conda.exe create -n writing-agent python=3.13 -y --solver=classic
C:\miniconda\envs\writing-agent\python.exe -m pip install -r requirements-dev.txt

Set-Location web
npm install
Set-Location ..

Copy-Item .env.example .env
# 编辑 .env，填写 OPENAI_API_KEY 等配置
```

## 启动 Web 工作台

生产模式由 FastAPI 同源托管构建后的前端：

```powershell
Set-Location D:\test_agent\writing-agent\web
npm run build

Set-Location ..
C:\miniconda\envs\writing-agent\python.exe -m uvicorn api.server:app --host 127.0.0.1 --port 8000
```

打开 `http://127.0.0.1:8000`。

前端开发模式使用两个终端：

```powershell
# 终端 1：API
C:\miniconda\envs\writing-agent\python.exe -m uvicorn api.server:app --host 127.0.0.1 --port 8000

# 终端 2：Vite
Set-Location D:\test_agent\writing-agent\web
npm run dev
```

开发页面为 `http://127.0.0.1:5173`。

## 常用命令

```powershell
# 单次写作任务
C:\miniconda\envs\writing-agent\python.exe -m agent run "写一篇关于模型蒸馏的文章" --assistant tech-writer

# 续接会话
C:\miniconda\envs\writing-agent\python.exe -m agent run "继续补充案例" --assistant tech-writer --resume <session_id>

# 助手管理
C:\miniconda\envs\writing-agent\python.exe -m agent assistants list
C:\miniconda\envs\writing-agent\python.exe -m agent assistants create marketing --name 营销文案 --description 短平快风格
C:\miniconda\envs\writing-agent\python.exe -m agent assistants delete marketing

# 长驻 Scheduler，任务定义见 config/settings.py
C:\miniconda\envs\writing-agent\python.exe -m agent schedule
```

Windows 登录时自动启动 Scheduler 的完整配置见 [docs/guides/windows-task-scheduler.md](docs/guides/windows-task-scheduler.md)。

## 验证

```powershell
C:\miniconda\envs\writing-agent\python.exe -m pytest tests -q
C:\miniconda\envs\writing-agent\python.exe -m pytest tests\test_memory_isolation.py -q

Set-Location web
npm test
npm run typecheck
npm run build
```

当前基线：Python `228/228`、记忆隔离 `10/10`、前端 `117/117`，类型检查与生产构建通过。

## 目录

| 目录 | 用途 |
|---|---|
| `agent/` | Agent Loop、Planner、Runtime、工具与 Skill 调度 |
| `memory/` | SQLite、项目、记忆、change set 和运行锁 |
| `api/` | FastAPI、SSE 与任务接口 |
| `web/` | Vue 3 + CodeMirror 写作 IDE |
| `scheduler/` | APScheduler 任务注册与派发 |
| `skills/` | research、writing、editing |
| `tests/` | Python 回归与记忆隔离红线 |
| `docs/` | 架构、审查结果和运维说明 |

## 更多文档

- [项目约定与 Agent 交接](AGENTS.md)
- [文档导航](docs/README.md)
- [架构 v1.26](docs/architecture/phase1-architecture.md)
- [后续待办](docs/guides/backlog.md)
- [审查记录索引](docs/README.md)
- [Windows Task Scheduler](docs/guides/windows-task-scheduler.md)
