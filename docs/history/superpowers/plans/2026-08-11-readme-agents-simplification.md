# README 与 AGENTS 精简实施计划



**Goal:** 将 README 改为快速上手入口，并压缩 AGENTS 的重复交接信息，同时保留运行说明和全部硬性规则。

**Architecture:** README 面向使用者，只承载首次安装、启动和常用操作；高级 Windows 自启说明下沉到独立文档。AGENTS 面向后续 Agent，只保留事实来源、环境、红线、流程和阶段门。

**Tech Stack:** Markdown、PowerShell、Git

---

### Task 1: 下沉 Windows Task Scheduler 说明

**Files:**
- Create: `docs/windows-task-scheduler.md`
- Modify: `README.md`

- [ ] 从当前 README 提取 APScheduler 与 Windows Task Scheduler 的 XML、导入、运行、查询和导出说明。
- [ ] 在独立文档中保留 `MultipleInstancesPolicy=IgnoreNew`、`misfire_grace_time=60`、用户 SID、Python 路径和工作目录约束。
- [ ] README 仅保留 `python -m agent schedule` 命令和详细文档链接。

### Task 2: 重写 README 快速上手版

**Files:**
- Modify: `README.md`

- [ ] 按“简介、核心能力、环境、安装、Web 启动、CLI、测试、目录、文档”顺序重写。
- [ ] 删除运行日志示例、阶段路线图和冗长机制表。
- [ ] 保留架构 v1.11 与 `123/123`、`9/9`、`30/30` 验证基线。
- [ ] 使用 `Get-Content README.md | Measure-Object -Line` 验证长度约 90 至 110 行。

### Task 3: 精简 AGENTS 交接文档

**Files:**
- Modify: `AGENTS.md`

- [ ] 合并当前状态、能力基线和阶段验收的重复信息。
- [ ] 保留必读顺序、指定 conda Python、MemoryStore/SQL 边界、assistant_id 隔离、密钥、阶段门、工作区保护和中文 Conventional Commits。
- [ ] 修正 Git 状态为已有提交、`main` 跟踪 `origin/main`。
- [ ] 使用 `Get-Content AGENTS.md | Measure-Object -Line` 验证长度约 70 至 85 行。

### Task 4: 文档验收并等待用户审查

**Files:**
- Verify: `README.md`
- Verify: `AGENTS.md`
- Verify: `docs/windows-task-scheduler.md`

- [ ] 使用 `rg` 检查架构版本、测试基线、Python 路径、阶段门、SQL 边界和 Git 提交规范仍然存在。
- [ ] 使用 `git diff --check` 检查新增空白错误，并查看 `git diff --stat`。
- [ ] 不执行提交或推送；向用户提供未提交差异摘要，等待审查确认。
