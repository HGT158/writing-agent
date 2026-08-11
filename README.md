# 个人写作 Agent（阶段 4）

专注内容生产的个人 Agent：网络检索 → 素材归纳 → 大纲 → 分段成文 → 质检 → Markdown 归档。
Planner 每轮**动态决定**调用哪些 Skill 和工具（选择理由全程可观测），不是固定流程的 Workflow。

架构设计见 `docs/phase1-architecture.md`（v1.11）。阶段 4 已实现本地 FastAPI + SSE + Vue 3 写作 IDE。

## 环境要求

- Windows 11，Python 3.12+
- Node.js 20+（构建/开发 Vue 3 Web UI，同时供 npx 启动 Tavily MCP server）
- LLM API Key：任意 OpenAI 兼容服务（默认 DeepSeek）
- Tavily API Key（可选，搜索用）

## 启动步骤

本项目使用 conda 环境 `writing-agent`（约定见 `AGENTS.md`）：

```bash
cd writing-agent

# 首次：创建 conda 环境并安装依赖（本机 conda 需禁用插件 + classic solver）
CONDA_NO_PLUGINS=true C:\miniconda\Scripts\conda.exe create -n writing-agent python=3.13 -y --solver=classic
C:\miniconda\envs\writing-agent\python.exe -m pip install -r requirements.txt

cd web
npm install
cd ..

copy .env.example .env   # 然后编辑 .env 填入 API Key
```

后续所有命令中的 `python` 均指 `C:\miniconda\envs\writing-agent\python.exe`（或 `conda activate writing-agent` 后直接用 `python`）。

## Web 写作工作台

生产模式先构建前端，再由 FastAPI 同源托管；API 只绑定本机 `127.0.0.1`：

```powershell
cd D:\test_agent\writing-agent\web
npm run build

cd ..
C:\miniconda\envs\writing-agent\python.exe -m uvicorn api.server:app --host 127.0.0.1 --port 8000
```

浏览器打开 `http://127.0.0.1:8000`。工作台支持：

- 一个助手管理多个文件夹项目；可新建空白项目，或导入 `.md`、`.markdown`、`.txt` 和整个文件夹。
- 导入内容复制到 `data/assistants/<assistant_id>/projects/<project_id>/`，不引用或同步外部原路径。
- CodeMirror 多标签编辑、Markdown 预览、乐观版本保存；桌面三栏，窄屏用项目/Agent 抽屉切换。
- 选中文本后弹出改写工具栏；AI 返回 diff，只有点击“接受”才写入文件。
- 右侧 Agent 面板绑定当前助手和项目；聊天修改同样先生成 change set，再接受或拒绝。

前端开发模式使用 Vite 代理 `/api`：

```powershell
# 终端 1
C:\miniconda\envs\writing-agent\python.exe -m uvicorn api.server:app --host 127.0.0.1 --port 8000

# 终端 2
cd D:\test_agent\writing-agent\web
npm run dev
```

开发页面为 `http://127.0.0.1:5173`。

## CLI 与 Scheduler

```bash
# 跑一个写作任务（run 子命令可省略）
C:\miniconda\envs\writing-agent\python.exe -m agent run "写一篇关于模型蒸馏的文章" --assistant tech-writer
C:\miniconda\envs\writing-agent\python.exe -m agent "润色一下 data/notes/draft.md 这篇文章"

# 续接会话
C:\miniconda\envs\writing-agent\python.exe -m agent run "继续补充案例分析" --assistant tech-writer --resume <session_id>

# 长驻运行 config/settings.py 中的 JOBS（内置任务绑定 default 助手，Ctrl+C 退出）
C:\miniconda\envs\writing-agent\python.exe -m agent schedule

# 助手管理
C:\miniconda\envs\writing-agent\python.exe -m agent assistants list
C:\miniconda\envs\writing-agent\python.exe -m agent assistants create marketing --name 营销文案 --description 短平快风格
C:\miniconda\envs\writing-agent\python.exe -m agent assistants delete marketing            # 归档（可恢复）
C:\miniconda\envs\writing-agent\python.exe -m agent assistants delete marketing --purge    # 级联清理

# 测试
C:\miniconda\envs\writing-agent\python.exe -m pytest tests/ -v -p no:cacheprovider --basetemp D:\test_agent\pytest-temp-writing-agent

cd web
npm test
npm run typecheck
npm run build
```

## Windows Task Scheduler 开机自启

APScheduler 运行在 `python -m agent schedule` 的 asyncio 事件循环中。Windows 只负责登录时启动这个长驻进程，具体 cron 时间仍由 `config/settings.py` 的 `JOBS` 管理；内置 `daily-ai-news` 任务绑定始终会自动创建的 `default` 助手。

先运行 `whoami /user` 取得当前用户 SID，把下面两处 `REPLACE_WITH_USER_SID` 替换为该 SID；如果项目路径或 conda 路径不同，同时修改 `Command` 和 `WorkingDirectory`。将内容保存为 `writing-agent-scheduler.xml`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>启动个人写作 Agent Scheduler</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>REPLACE_WITH_USER_SID</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>REPLACE_WITH_USER_SID</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>C:\miniconda\envs\writing-agent\python.exe</Command>
      <Arguments>-m agent schedule</Arguments>
      <WorkingDirectory>D:\test_agent\writing-agent</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
```

导入、立即测试和导出备份：

```powershell
schtasks /Create /TN "WritingAgentScheduler" /XML ".\writing-agent-scheduler.xml" /F
schtasks /Run /TN "WritingAgentScheduler"
schtasks /Query /TN "WritingAgentScheduler" /V /FO LIST
schtasks /Query /TN "WritingAgentScheduler" /XML > ".\writing-agent-scheduler-exported.xml"
```

`MultipleInstancesPolicy=IgnoreNew` 防止重复登录或手动触发时启动第二个 Scheduler 进程。单个助手的实际任务互斥仍由 `app.db` 中的 `run_locks` 保证。Scheduler 使用 `misfire_grace_time=60`：电脑休眠或进程停止导致触发时间错过超过 60 秒时，该次任务会跳过而不补跑，避免恢复后集中执行过期任务。

## 运行日志示例

```
[信息] 工具表就绪：内置 3 + MCP 4
[信息] 助手「科技作者」开始任务（session 3f9a2c1b7d04）
[思考] 任务涉及技术事实与最新进展，需要先搜集资料再成文
  └ 激活 Skill：research —— 任务需要外部事实与可引用来源
[Skill] 已激活「research」：任务需要外部事实与可引用来源
[思考] 拆解为 3 个查询词并行搜索
  └ 工具选择理由：tavily_search 覆盖网页时效信息，三个查询词相互独立可并行
[调用工具] tavily_search  args={"query": "模型蒸馏 原理 2026"}
[调用工具] tavily_search  args={"query": "knowledge distillation survey"}
[调用工具] tavily_search  args={"query": "DeepSeek R1 蒸馏 小模型"}
[工具结果] tavily_search 成功：找到 5 条结果：1. Knowledge Distillation: A Survey…
[思考] 已获 8 个候选来源，抓取其中 2 篇全文核实关键数据
[调用工具] fetch  args={"url": "https://arxiv.org/abs/2006.05525"}
[工具结果] fetch 成功：Knowledge Distillation: A Survey 全文 4523 字符…
[思考] 素材覆盖原理/方法/代表案例，激活 writing 进入成文
  └ 激活 Skill：writing —— 素材齐备，需要结构化长文产出
[思考] 大纲：《模型蒸馏：让小模型站上巨人的肩膀》 背景与动机 / 核心机制 / 主流方法对比 / 代表实践 / 趋势判断
[成文] 正在撰写：背景与动机
……（token 流式输出）……
[思考] 质检通过
[完成] 文章已保存：data/articles/tech-writer/模型蒸馏：让小模型站上巨人的肩膀-20260806-1030.md
```

## 关键机制速查

| 机制 | 位置 | 说明 |
|------|------|------|
| Planner 动态决策 | `agent/planner.py` | 每轮输出 ActionPlan（含 Skill/工具选择理由），条件边按 next_action 路由 |
| Skill 渐进式披露 | `skills/*/SKILL.md` | 启动只读 frontmatter，激活才注入正文；`tools.yaml` 依赖激活前校验 |
| 统一工具表 | `agent/executor.py` | 内置工具与 MCP 工具同一 ToolSpec 协议；ToolContext 隐式注入 assistant_id |
| MCP | `mcp_client/` | 官方 SDK stdio；配置见 `config/mcp_servers.json`（`${VAR}` 插值为本实现超集） |
| 记忆隔离 | `memory/store.py` | 全部接口强制 assistant_id；profile.md 按助手目录物理隔离 |
| 中文记忆检索 | `memory/short_term.py` | messages/articles 使用 FTS5 trigram；无三字词元时回退最多 16 项字面量 LIKE，检索分路故障不阻断任务 |
| 运行锁 | `memory/store.py` | run_locks 表跨进程互斥，TTL + PID 存活校验回收崩溃残留 |
| 定时任务 | `scheduler/` | AsyncIOScheduler 与 Runtime 共用事件循环，job 绑定 assistant_id |
| 项目与 change set | `memory/projects.py` | 受管项目目录、文档版本、原子保存、归档/purge、AI 修改建议状态机 |
| Web API / SSE | `api/` | 助手、普通任务、项目、文档、完成态文章和 change set 接口；TaskBroker 归档事件流 |
| 写作 IDE | `web/` | Vue 3 + CodeMirror；项目资源树、多标签编辑、Markdown 预览、选区改写和 Agent 面板 |
| 降级路径 | `agent/planner.py` | Planner 连续非法 JSON → 强制可路由的 finish，绝不卡死状态机 |

## 常见问题

- **提示"MCP server tavily 启动失败"**：未装 Node 或未配 TAVILY_API_KEY。此时 research skill 激活会失败并告知 Planner，Agent 会基于已有知识直接成文（或提示你配置）。
- **提示"助手 X 正忙"**：同一助手同时只允许一个任务（跨进程锁），等当前任务完成或检查是否有残留进程。
- **想清空所有记忆**：删除 `data/` 目录即可（助手定义在 `data/assistants/`，注意备份）。

## 阶段路线图

- 阶段 2：CLI + Agent Loop + Skill + MCP + 记忆隔离 + Markdown 产出
- 阶段 3：recall 升级 FTS5 trigram 检索、APScheduler 定时任务（已完成）
- 阶段 4：FastAPI + SSE + Vue 3 写作 IDE（一助手多文章项目、复制导入、选区/聊天 change set；已完成）

当前验证基线：Python `123/123`、记忆隔离红线 `9/9`、前端 `30/30`；类型检查与 Vite 生产构建通过。
