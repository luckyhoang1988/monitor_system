"""Tài liệu agent/skill có 2 bản: `.claude/` (nguồn) và `.codex/` + `.agents/` (cho Codex).

Hai bản từng lệch nhau lâu ngày (cùng chép nội dung lỗi thời `config/devices.yaml`, HP/Aruba, Grafana…).
Test này chỉ khoá việc "2 bản phải giống nhau" và "bộ file phải đủ"; đúng/sai của nội dung do người sửa
chịu trách nhiệm. Danh sách bắt buộc nằm CỨNG ở EXPECTED_* — nếu chỉ duyệt thư mục thì xoá một file sẽ
làm test tương ứng biến mất và suite vẫn xanh.

Sửa `.claude/agents/*.md` hoặc `.claude/commands/*.md` thì sửa luôn bản tương ứng ở `.codex/agents/*.toml`
và `.agents/skills/source-command-*/SKILL.md` (xem CLAUDE.md "Thay đổi quan trọng"). Thêm agent/command
mới vào đồng bộ thì thêm tên vào EXPECTED_* ở đây.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = ROOT / ".claude" / "agents"
COMMANDS_DIR = ROOT / ".claude" / "commands"
CODEX_AGENTS_DIR = ROOT / ".codex" / "agents"
SKILLS_DIR = ROOT / ".agents" / "skills"
SKILL_PREFIX = "source-command-"

EXPECTED_AGENTS = (
    "alerting-agent",
    "dashboard-agent",
    "hyperv-collector-agent",
    "infra-design-agent",
    "switch-collector-agent",
)
# `deploy.md` cố ý không có bản skill cho Codex nên không nằm ở đây.
EXPECTED_COMMANDS = (
    "add-device",
    "design-collector",
    "plan-phase",
    "research-oids",
)


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def _split_frontmatter(md: str) -> tuple[dict[str, str], str]:
    assert md.startswith("---\n"), "thiếu frontmatter"
    head, body = md[4:].split("\n---\n", 1)
    fields = {}
    for line in head.split("\n"):
        key, sep, value = line.partition(":")
        if sep and not line.startswith((" ", "-")):
            fields[key.strip()] = value.strip()
    return fields, body.strip("\n")


def _agent_names() -> list[str]:
    return sorted(set(EXPECTED_AGENTS) | {p.stem for p in AGENTS_DIR.glob("*.md")})


def _command_names() -> list[str]:
    mirrored = {
        p.stem for p in COMMANDS_DIR.glob("*.md") if (SKILLS_DIR / f"{SKILL_PREFIX}{p.stem}").exists()
    }
    return sorted(set(EXPECTED_COMMANDS) | mirrored)


def test_expected_lists_are_not_empty_and_match_what_the_repo_ships():
    assert EXPECTED_AGENTS and EXPECTED_COMMANDS
    shipped_agents = {p.stem for p in AGENTS_DIR.glob("*.md")}
    assert set(EXPECTED_AGENTS) <= shipped_agents, f"thiếu agent: {set(EXPECTED_AGENTS) - shipped_agents}"
    shipped_commands = {p.stem for p in COMMANDS_DIR.glob("*.md")}
    assert set(EXPECTED_COMMANDS) <= shipped_commands, f"thiếu command: {set(EXPECTED_COMMANDS) - shipped_commands}"


@pytest.mark.parametrize("name", _agent_names())
def test_codex_agent_matches_claude_agent(name: str):
    md_path = AGENTS_DIR / f"{name}.md"
    toml_path = CODEX_AGENTS_DIR / f"{name}.toml"
    assert md_path.exists(), f"thiếu {md_path.relative_to(ROOT)}"
    assert toml_path.exists(), f"thiếu {toml_path.relative_to(ROOT)}"

    fields, body = _split_frontmatter(_read(md_path))
    codex = tomllib.loads(_read(toml_path))

    assert codex["name"] == fields["name"] == name
    assert codex["description"] == fields["description"]
    assert codex["developer_instructions"].strip("\n") == body


@pytest.mark.parametrize("name", _command_names())
def test_codex_skill_embeds_claude_command(name: str):
    md_path = COMMANDS_DIR / f"{name}.md"
    skill_path = SKILLS_DIR / f"{SKILL_PREFIX}{name}" / "SKILL.md"
    assert md_path.exists(), f"thiếu {md_path.relative_to(ROOT)}"
    assert skill_path.exists(), f"thiếu {skill_path.relative_to(ROOT)}"

    skill = _read(skill_path)
    marker = "## Command Template\n\n"
    assert marker in skill
    assert skill.split(marker, 1)[1].strip("\n") == _read(md_path).strip("\n")


def test_no_orphan_codex_files():
    """Bản Codex mồ côi (nguồn `.claude/` đã bị xoá) cũng là lệch."""
    claude_agents = {p.stem for p in AGENTS_DIR.glob("*.md")}
    orphan_agents = {p.stem for p in CODEX_AGENTS_DIR.glob("*.toml")} - claude_agents
    assert not orphan_agents, f".codex/agents không có nguồn trong .claude/agents: {orphan_agents}"

    claude_commands = {p.stem for p in COMMANDS_DIR.glob("*.md")}
    skill_commands = {
        d.name[len(SKILL_PREFIX):] for d in SKILLS_DIR.iterdir() if d.is_dir() and d.name.startswith(SKILL_PREFIX)
    }
    orphan_skills = skill_commands - claude_commands
    assert not orphan_skills, f".agents/skills không có nguồn trong .claude/commands: {orphan_skills}"


def test_agents_md_points_to_claude_md_instead_of_copying_it():
    agents_md = _read(ROOT / "AGENTS.md")
    assert "CLAUDE.md" in agents_md
    assert len(agents_md.splitlines()) < 150, "AGENTS.md phải là bản mỏng, không phải bản sao CLAUDE.md"
