"""CLI 入口（架构 §5.4）：

  python -m agent run "写一篇关于 X 的文章" --assistant tech-writer
  python -m agent "写一篇关于 X 的文章"            # run 可省略
  python -m agent run "..." --resume <session_id>
  python -m agent schedule
  python -m agent assistants list|create|edit|delete
  python -m agent assistants create editor --persona-file persona.txt
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from config.settings import Settings, load_settings
from memory.store import AssistantBusyError

from .events import EventBus, console_printer
from .runtime import AgentRuntime

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m agent", description="个人写作 Agent（阶段 3）")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="运行一次写作任务")
    run.add_argument("task", help="写作任务描述")
    run.add_argument("--assistant", default="default", help="助手 id（缺省 default）")
    run.add_argument("--resume", default=None, help="续接的 session_id")

    sub.add_parser("schedule", help="长驻运行 config/settings.py 中的定时任务")

    assistants = sub.add_parser("assistants", help="助手管理")
    assistants.add_argument("action", choices=["list", "create", "edit", "delete"])
    assistants.add_argument("id", nargs="?", default=None)
    assistants.add_argument("--name", default=None)
    assistants.add_argument("--description", default=None)
    assistants.add_argument("--purge", action="store_true", help="删除时级联清理 SQL 数据与归档目录")
    persona_args = assistants.add_mutually_exclusive_group()
    persona_args.add_argument(
        "--persona", default=None,
        help="系统提示词正文（create/edit；空白落为默认人设）",
    )
    persona_args.add_argument(
        "--persona-file", default=None,
        help="从 UTF-8 文本文件读取系统提示词（create/edit；与 --persona 互斥）",
    )
    return parser


async def _cmd_run(args: argparse.Namespace) -> int:
    settings = load_settings()
    bus = EventBus()
    bus.subscribe(console_printer)
    runtime = AgentRuntime(settings, bus)
    try:
        await runtime.start()
        final = await runtime.run(args.assistant, args.task, session_id=args.resume)
    except AssistantBusyError as exc:
        bus.emit("failed", reason=str(exc))
        return 2
    except KeyError as exc:
        bus.emit("failed", reason=str(exc).strip("'"))
        return 2
    except RuntimeError as exc:  # 未配置 API Key 等可预期错误
        bus.emit("failed", reason=str(exc))
        return 2
    except Exception as exc:
        logger.exception("Agent CLI 运行失败")
        bus.emit("failed", reason=f"运行失败：{exc}")
        return 2
    finally:
        await runtime.close()
    return 0 if final.get("status") == "done" else 1


async def _cmd_schedule() -> int:
    settings = load_settings()
    bus = EventBus()
    bus.subscribe(console_printer)
    runtime = AgentRuntime(settings, bus)
    try:
        await runtime.start(enable_scheduler=True)
        bus.emit("info", text="Scheduler 长驻运行中，按 Ctrl+C 退出")
        await asyncio.Event().wait()
    finally:
        await runtime.close()
    return 0


def _read_persona(args: argparse.Namespace) -> str | None:
    """--persona 与 --persona-file 互斥由 argparse 约束；文件按 UTF-8 读取。"""
    if args.persona_file is not None:
        try:
            return Path(args.persona_file).read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            # 非 UTF-8 文件给可读提示，不再是裸 codec traceback 摘要（phase10 P3-2）
            raise ValueError(
                f"persona 文件必须是 UTF-8 文本：{args.persona_file}（{exc}）"
            ) from exc
    return args.persona


def _cmd_assistants(args: argparse.Namespace, settings: Settings | None = None) -> int:
    settings = settings or load_settings()
    runtime = AgentRuntime(settings)
    try:
        if args.action == "list":
            for a in runtime.assistants.list():
                locked = "（任务运行中）" if runtime.store.is_locked(a.id) else ""
                print(f"- {a.id}：{a.name}{locked}  {a.description}")
        elif args.action == "create":
            if not args.id:
                print("create 需要助手 id", file=sys.stderr)
                return 2
            a = runtime.assistants.create(
                args.id, args.name or args.id, args.description or "",
                persona=_read_persona(args),
            )
            print(f"已创建助手：{a.id}（{a.directory}）")
        elif args.action == "edit":
            if not args.id:
                print("edit 需要助手 id", file=sys.stderr)
                return 2
            if args.name is not None and not args.name.strip():
                print("失败：显示名不能为空", file=sys.stderr)
                return 2
            a = runtime.assistants.update(
                args.id,
                name=args.name,
                description=args.description,
                persona=_read_persona(args),
            )
            print(f"已更新助手：{a.id}（{a.directory}）")
        elif args.action == "delete":
            if not args.id:
                print("delete 需要助手 id", file=sys.stderr)
                return 2
            target = runtime.assistants.delete(args.id, purge=args.purge)
            print(f"已{'级联删除' if args.purge else '归档到'}：{target}")
    except (KeyError, ValueError, RuntimeError, OSError) as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 2
    finally:
        runtime.store.close()
    return 0


def main() -> int:
    argv = sys.argv[1:]
    known = {"run", "schedule", "assistants", "-h", "--help"}
    if argv and argv[0] not in known:
        argv = ["run", *argv]  # python -m agent "任务" --assistant X 等价于 run 子命令
    elif not argv:
        argv = ["-h"]
    args = _build_parser().parse_args(argv)

    if args.command == "run":
        return asyncio.run(_cmd_run(args))
    if args.command == "schedule":
        try:
            return asyncio.run(_cmd_schedule())
        except KeyboardInterrupt:
            return 130
    if args.command == "assistants":
        return _cmd_assistants(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
