"""事件总线：思考过程 / 工具调用 / token 流统一分发（架构 §6.2）。

阶段 2 由 console_printer 打印到终端；阶段 4 增加 SSE 订阅者，Runtime 零改动。
"""
from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable

import colorama
from colorama import Fore, Style

colorama.init()

logger = logging.getLogger(__name__)

Event = dict[str, Any]
Subscriber = Callable[[Event], None]
_TASK_ID: ContextVar[str | None] = ContextVar("event_task_id", default=None)


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []

    def subscribe(self, fn: Subscriber) -> None:
        self._subscribers.append(fn)

    def emit(self, event_type: str, **data: Any) -> None:
        event: Event = {"type": event_type, "data": data}
        task_id = _TASK_ID.get()
        if task_id is not None:
            event["task_id"] = task_id
        for fn in self._subscribers:
            try:
                fn(event)
            except Exception:
                logger.debug("事件订阅者异常（不影响主流程）", exc_info=True)

    @contextmanager
    def task_scope(self, task_id: str):
        token = _TASK_ID.set(task_id)
        try:
            yield
        finally:
            _TASK_ID.reset(token)



def _truncate(text: str, limit: int = 200) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def console_printer(event: Event) -> None:
    """阶段 2 的终端可视化：Planner 理由高亮，工具调用/结果分行，token 流不换行。"""
    etype = event["type"]
    data = event["data"]

    if etype == "thought":
        print(f"\n{Fore.CYAN}[思考]{Style.RESET_ALL} {data.get('text', '')}")
        if data.get("skill"):
            print(f"  {Fore.CYAN}└ 激活 Skill：{data['skill']}{Style.RESET_ALL} —— {data.get('skill_reason', '')}")
        if data.get("tool_reason"):
            print(f"  {Fore.CYAN}└ 工具选择理由：{data['tool_reason']}{Style.RESET_ALL}")
    elif etype == "tool_call":
        print(f"{Fore.YELLOW}[调用工具]{Style.RESET_ALL} {data.get('tool')}  args={_truncate(data.get('args'), 120)}")
    elif etype == "tool_result":
        if data.get("ok"):
            print(f"{Fore.GREEN}[工具结果]{Style.RESET_ALL} {data.get('tool')} 成功：{_truncate(data.get('summary', ''))}")
        else:
            print(f"{Fore.RED}[工具结果]{Style.RESET_ALL} {data.get('tool')} 失败：{_truncate(data.get('error', ''))}")
    elif etype == "skill":
        print(f"{Fore.MAGENTA}[Skill]{Style.RESET_ALL} {data.get('text', '')}")
    elif etype == "section":
        print(f"\n{Fore.MAGENTA}[成文]{Style.RESET_ALL} 正在撰写：{data.get('title', '')}")
    elif etype == "token":
        print(f"{Style.DIM}{data.get('text', '')}{Style.RESET_ALL}", end="", flush=True)
    elif etype == "done":
        print(f"\n{Fore.GREEN}{Style.BRIGHT}[完成]{Style.RESET_ALL} 文章已保存：{data.get('path', '')}")
    elif etype == "failed":
        print(f"\n{Fore.RED}{Style.BRIGHT}[失败]{Style.RESET_ALL} {data.get('reason', '')}")
    elif etype == "warning":
        print(f"{Fore.YELLOW}[警告]{Style.RESET_ALL} {data.get('text', '')}", file=sys.stderr)
    elif etype == "info":
        print(f"{Style.DIM}[信息] {data.get('text', '')}{Style.RESET_ALL}")
