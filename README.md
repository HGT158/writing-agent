# 个人写作 Agent

面向内容生产的本地写作 Agent：网络检索、素材归纳、大纲规划、分段成文、质量检查和 Markdown 归档。
Planner 每轮动态选择 Skill 与工具，不是固定 Workflow。当前已完成阶段 4：FastAPI + SSE + Vue 3 写作工作台。

架构单一事实来源：[docs/architecture/phase1-architecture.md](docs/architecture/phase1-architecture.md)（v1.12）。

## 核心能力

- 多助手及独立 persona、Skill、记忆和文章目录。
- LangGraph 六节点 Agent Loop，支持 MCP、流式事件和跨任务记忆。
- 一助手多文章项目，支持新建或复制导入文本文件和文件夹。
- CodeMirror 多标签编辑、Markdown 预览和乐观版本保存。
- 选中文本后输入提示词局部改写，接受后才写入文件。
- 右侧 Agent 面板支持项目级聊天修改和 change set 审核。
- APScheduler 定时任务与跨进程助手运行锁。

## 环境要求

- Windows 11
- Python 3.12+，固定使用 `C:\miniconda\envs\writing-agent\python.exe`
- Node.js 20+
- OpenAI 兼容 LLM API Key；Tavily API Key 可选

## 快速安装

```powershell
cd D:\test_agent\writing-agent

# 本机 conda 需要禁用插件并使用 classic solver
$env:CONDA_NO_PLUGINS = "true"
C:\miniconda\Scripts\conda.exe create -n writing-agent python=3.13 -y --solver=classic
C:\miniconda\envs\writing-agent\python.exe -m pip install -r requirements.txt

Set-Location web
npm install
Set-Location ..

Copy-Item .env.example .env
# 编辑 .env，填写 LLM_API_KEY 等配置
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

当前基线：Python `123/123`、记忆隔离 `9/9`、前端 `30/30`，类型检查与生产构建通过。

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
- [架构 v1.12](docs/architecture/phase1-architecture.md)
- [阶段 4 复审处理结果](docs/reviews/phase4-code-review.md)
- [Windows Task Scheduler](docs/guides/windows-task-scheduler.md)
