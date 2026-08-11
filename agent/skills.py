"""Skill 系统（架构 §5.5）：对齐 Claude Code 的渐进式披露。

启动时只解析 SKILL.md 的 YAML frontmatter（name/description/when_to_use），
正文 prompt 在 Planner 激活后才注入；tools.yaml 声明依赖工具，激活前校验可用性。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Skill:
    name: str
    description: str
    when_to_use: str
    body: str
    tools: list[str] = field(default_factory=list)
    path: Path | None = None


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def _parse_skill_md(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise ValueError(f"{path} 缺少 YAML frontmatter")
    meta = yaml.safe_load(match.group(1)) or {}
    body = text[match.end():].strip()

    tools: list[str] = []
    tools_yaml = path.parent / "tools.yaml"
    if tools_yaml.exists():
        raw = yaml.safe_load(tools_yaml.read_text(encoding="utf-8")) or {}
        tools = list(raw.get("tools", []))

    return Skill(
        name=meta.get("name", path.parent.name),
        description=meta.get("description", ""),
        when_to_use=meta.get("when_to_use", ""),
        body=body,
        tools=tools,
        path=path,
    )


def load_skills(skills_dir: Path) -> dict[str, Skill]:
    """扫描 skills/ 目录动态注册（架构 §5.5）。"""
    skills: dict[str, Skill] = {}
    if not skills_dir.exists():
        return skills
    for child in sorted(skills_dir.iterdir()):
        skill_md = child / "SKILL.md"
        if child.is_dir() and skill_md.exists():
            skill = _parse_skill_md(skill_md)
            skills[skill.name] = skill
    return skills


def missing_dependencies(skill: Skill, available_tools: set[str]) -> list[str]:
    """激活前校验：返回缺失的依赖工具列表（空 = 可激活）。"""
    return [t for t in skill.tools if t not in available_tools]


def catalog_text(skills: dict[str, Skill], allowed: list[str] | None) -> str:
    """给 Planner 的 Skill 清单（只含元数据，不注入正文）。"""
    lines = []
    for skill in skills.values():
        if allowed is not None and skill.name not in allowed:
            continue
        lines.append(f"- {skill.name}：{skill.description}（何时使用：{skill.when_to_use}）")
    return "\n".join(lines) or "(无可用 Skill)"
