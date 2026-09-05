"""项目聊天的记忆沉淀策略（架构 §5.4 v1.30）。

选择性沉淀固定三步：确定性信号门槛（零成本）→ 显式指令直达 memorize
（零模型调用）→ 其余命中用一次非流式 JSON 提取（每轮至多一次）。
本模块不直接触存储与工作记录，由 Runtime 接线并统一降级。
"""
from __future__ import annotations

import asyncio
import json
import re

from .llm import chat_text, extract_json

# 显式指令：用户点名让助手记住的内容，剥离指令词后直接落库，不调模型。
_EXPLICIT_COMMAND = re.compile(
    r"^(?:请)?(?:帮我)?(?:记住|记一下|记下来)(?:这个)?[，,。：:！!？?\s]*(.+)$",
    re.DOTALL,
)

# 偏好反馈信号：命中才进入沉淀裁决；未命中不写画像、不调模型。
# 门槛只负责"值得看一眼"，是否真有持久偏好由提取调用裁决。
_SIGNAL_PATTERN = re.compile(
    r"记住|记一下|记下来"
    r"|我喜欢|我更喜欢|我不喜欢|我不想要|我倾向"
    r"|不要用|别用|以后都|以后请|每次都|一律"
    r"|文风|语气|口吻|称呼|风格"
    r"|太啰嗦|太长|太短|太正式|太口语|口语化|正式一点|书面一点"
    r"|标点|感叹号"
)

_KIND_LABELS = {"preference": "偏好", "style": "风格", "topic": "常用主题"}
MAX_EXTRACTED_ITEMS = 3
MAX_ITEM_CHARS = 120
# 提取 prompt 携带的画像上限（phase10 P1-3）：全文注入会放大每次沉淀成本。
MAX_PROFILE_IN_PROMPT_CHARS = 8_000


def has_consolidation_signal(message: str) -> bool:
    return bool(_SIGNAL_PATTERN.search(message or ""))


def extract_explicit_command(message: str) -> str | None:
    """显式指令且带正文时返回正文；其余情况返回 None 走启发式提取。

    直达边界（phase10 P1-2）：超长（>MAX_ITEM_CHARS）或含换行的内容不直达——
    截断会产生无意义片段，交启发式提取由模型裁决；疑问语气（结尾 吗/呢/？/?）
    不直达，避免把「记住我要写什么了吗？」类提问当偏好直存。
    """
    match = _EXPLICIT_COMMAND.match(message.strip()) if message else None
    if not match:
        return None
    content = match.group(1).strip()
    if not content:
        return None
    if len(content) > MAX_ITEM_CHARS or "\n" in content:
        return None
    if content.endswith(("？", "?", "吗", "呢")):
        return None
    return content


# 画像既有条目形如「- [偏好] {content} （时间戳）」，剥离条目装饰后取正文比对。
_PROFILE_ENTRY_RE = re.compile(r"^-\s*\[[^\]]+\]\s*(.*?)\s*（[^）]*）\s*$")
# 手改条目可能缺时间戳后缀：回退为只剥条目头（- [kind] ），正文原样比对。
_PROFILE_ENTRY_PREFIX_RE = re.compile(r"^-\s*\[[^\]]+\]\s*")


def is_duplicate_memory(content: str, profile_text: str) -> bool:
    """规范化（去除全部空白）后与画像既有条目逐条比对；空内容视为重复不写入。

    等价判定是「剥离条目装饰后整条相等」而非跨条目子串包含（phase10 复审补强）：
    「简洁」这类短新偏好是更长既有条目的子串，包含语义会把它误判为重复而
    静默吞掉；同一句话重复说的场景整条相等已覆盖（规范化抵消空白差异）。
    """
    needle = "".join(str(content).split())
    if not needle:
        return True
    for line in str(profile_text or "").splitlines():
        stripped = line.strip()
        match = _PROFILE_ENTRY_RE.match(stripped)
        existing = match.group(1) if match else _PROFILE_ENTRY_PREFIX_RE.sub("", stripped)
        if "".join(existing.split()) == needle:
            return True
    return False


def kind_label(kind: str) -> str:
    return _KIND_LABELS.get(kind, "偏好")


async def extract_preferences(
    llm,
    model: str,
    *,
    user_message: str,
    assistant_reply: str,
    profile_text: str,
    json_mode: bool = True,
    temperature: float = 0.3,
    timeout_seconds: float | None = None,
) -> list[tuple[str, str]]:
    """一次非流式提取调用；返回 [(kind, content)]，无可沉淀内容返回空列表。

    timeout_seconds 提供独立短超时（phase10 P2-7）：无界调用会拖住任务终态
    与助手锁；画像全文不进 prompt，只带截取片段（phase10 P1-3）。
    """
    system = "你在为写作助手维护长期记忆，从一轮对话中提取值得长期保留的用户偏好。只输出 JSON。"
    prompt = (
        "判定标准：只有表达持久取向或约束的反馈才沉淀（如文风语气、句式与篇幅、"
        "结构安排、禁用词、称呼习惯、常写主题）；一次性的编辑指令、事实问答、寒暄"
        "不要沉淀；与现有画像等价的记录不要重复输出。\n\n"
        f"现有长期画像（可能为空，超长已截取）：\n"
        f"{(profile_text or '')[:MAX_PROFILE_IN_PROMPT_CHARS] or '（空）'}\n\n"
        f"当轮用户消息：{user_message}\n\n"
        f"当轮助手回复：{(assistant_reply or '')[:2000]}\n\n"
        '输出 JSON：{"items": [{"kind": "preference|style|topic", '
        '"content": "不超过120字的一句话陈述"}]}，没有可沉淀内容时输出 {"items": []}。'
    )
    call = chat_text(
        llm, model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        temperature=temperature,
        json_mode=json_mode,
    )
    if timeout_seconds is not None:
        text = await asyncio.wait_for(call, timeout_seconds)
    else:
        text = await call
    result = json.loads(extract_json(text))
    raw_items = result.get("items", [])
    if not isinstance(raw_items, list):
        return []
    extracted: list[tuple[str, str]] = []
    for item in raw_items[:MAX_EXTRACTED_ITEMS]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "preference"))
        if kind not in _KIND_LABELS:
            kind = "preference"
        content = str(item.get("content", "")).strip()
        if content:
            extracted.append((kind, content[:MAX_ITEM_CHARS]))
    return extracted
