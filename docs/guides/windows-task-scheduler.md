# Windows Task Scheduler 配置

APScheduler 运行在 `python -m agent schedule` 的 asyncio 事件循环中。Windows 只负责登录时启动长驻进程，具体 cron 时间由 `config/settings.py` 的 `JOBS` 管理。

## 创建任务 XML

先运行 `whoami /user` 获取当前用户 SID，将 XML 中两处 `REPLACE_WITH_USER_SID` 替换为该值。如果本机路径不同，同时修改 `Command` 和 `WorkingDirectory`。

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
      <Command>D:\VSC-Project\writing-agent\.venv\Scripts\python.exe</Command>
      <Arguments>-m agent schedule</Arguments>
      <WorkingDirectory>D:\VSC-Project\writing-agent</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
```

将内容保存为 `writing-agent-scheduler.xml`。

## 导入与验证

```powershell
schtasks /Create /TN "WritingAgentScheduler" /XML ".\writing-agent-scheduler.xml" /F
schtasks /Run /TN "WritingAgentScheduler"
schtasks /Query /TN "WritingAgentScheduler" /V /FO LIST
schtasks /Query /TN "WritingAgentScheduler" /XML > ".\writing-agent-scheduler-exported.xml"
```

`MultipleInstancesPolicy=IgnoreNew` 防止重复登录或手动触发时启动第二个 Scheduler 进程。单个助手的任务互斥由 `data/app.db` 中的 `run_locks` 保证。

Scheduler 使用 `misfire_grace_time=60`。电脑休眠或进程停止导致触发时间错过超过 60 秒时，该次任务会跳过而不补跑，避免恢复后集中执行过期任务。
