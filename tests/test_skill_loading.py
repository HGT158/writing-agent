"""Skill 动态加载测试：SKILL.md frontmatter 解析、tools.yaml 依赖、激活前校验。"""
from pathlib import Path

from agent.skills import catalog_text, load_skills, missing_dependencies

SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"


def test_load_three_builtin_skills():
    skills = load_skills(SKILLS_DIR)
    assert set(skills) == {"research", "writing", "editing"}
    for skill in skills.values():
        assert skill.description and skill.when_to_use
        assert skill.body  # 渐进式披露：正文已解析但默认不注入 Planner


def test_skill_dependencies_declared():
    skills = load_skills(SKILLS_DIR)
    assert set(skills["research"].tools) == {"tavily_search", "fetch"}
    assert "finalize_article" in skills["writing"].tools


def test_missing_dependency_check():
    skills = load_skills(SKILLS_DIR)
    assert missing_dependencies(skills["research"], {"tavily_search", "fetch"}) == []
    assert missing_dependencies(skills["research"], {"fetch"}) == ["tavily_search"]


def test_catalog_respects_assistant_subset():
    skills = load_skills(SKILLS_DIR)
    full = catalog_text(skills, None)
    subset = catalog_text(skills, ["writing"])
    assert "research" in full
    assert "research" not in subset and "writing" in subset
