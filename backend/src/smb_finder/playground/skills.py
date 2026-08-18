"""Codex 호환 SKILL.md 저장소와 Playground slash command 포매터."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from .models import SkillDefinition

if TYPE_CHECKING:
    from .tools import ToolHandler

_logger = logging.getLogger(__name__)
_SKILL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_BUILTIN_ROOT = Path(__file__).resolve().parent / "builtin_skills"
_CATEGORY_NAMES = {
    "smb": "SMB 직접 접근",
    "database": "DB 접근",
    "report": "보고서 관련",
    "skill": "Skill 관리",
}


class SkillStoreError(ValueError):
    """SKILL.md 형식 또는 저장 작업 오류."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _unquote_metadata(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
        return parsed if isinstance(parsed, str) else value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    return value


def parse_skill_document(skill_id: str, document: str, *, source: str) -> SkillDefinition:
    """표준 frontmatter와 본문을 가진 SKILL.md를 검증하고 해석한다."""

    if not _SKILL_ID_RE.fullmatch(skill_id):
        raise SkillStoreError("invalid_skill_id", "skill id는 소문자 영문·숫자·하이픈만 사용할 수 있습니다.")
    normalized = document.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(normalized) > 20_000:
        raise SkillStoreError("skill_too_large", "SKILL.md는 20,000자를 넘을 수 없습니다.")
    if not normalized.startswith("---\n"):
        raise SkillStoreError("invalid_skill_frontmatter", "SKILL.md는 --- frontmatter로 시작해야 합니다.")
    closing = normalized.find("\n---\n", 4)
    if closing < 0:
        raise SkillStoreError("invalid_skill_frontmatter", "SKILL.md frontmatter의 닫는 ---가 필요합니다.")

    metadata: dict[str, str] = {}
    for line in normalized[4:closing].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition(":")
        if not separator:
            raise SkillStoreError("invalid_skill_frontmatter", "frontmatter는 key: value 형식이어야 합니다.")
        metadata[key.strip()] = _unquote_metadata(value)

    name = metadata.get("name", "").strip()
    description = metadata.get("description", "").strip()
    instructions = normalized[closing + 5 :].strip()
    if name != skill_id:
        raise SkillStoreError("skill_name_mismatch", "frontmatter name은 skill id와 같아야 합니다.")
    if not description:
        raise SkillStoreError("skill_description_missing", "frontmatter description이 필요합니다.")
    if not instructions:
        raise SkillStoreError("skill_instructions_missing", "SKILL.md 본문 지침이 필요합니다.")
    return SkillDefinition(
        id=skill_id,
        name=name,
        description=description,
        instructions=instructions,
        document=f"{normalized}\n",
        source=source,
        editable=source == "user",
    )


class SkillStore:
    """버전 관리 기본 스킬과 로컬 사용자 스킬을 같은 SKILL.md 형식으로 관리한다."""

    def __init__(self, user_root: str | Path):
        self.user_root = Path(user_root)

    def list(self) -> tuple[SkillDefinition, ...]:
        skills: dict[str, SkillDefinition] = {}
        for skill in self._load_root(_BUILTIN_ROOT, source="builtin"):
            skills[skill.id] = skill
        for skill in self._load_root(self.user_root, source="user"):
            if skill.id in skills:
                _logger.warning("기본 skill과 id가 겹쳐 사용자 skill을 건너뜀: %s", skill.id)
                continue
            skills[skill.id] = skill
        return tuple(skills[skill_id] for skill_id in sorted(skills))

    def get(self, skill_id: str) -> SkillDefinition | None:
        return next((skill for skill in self.list() if skill.id == skill_id), None)

    def create(self, skill_id: str, document: str) -> SkillDefinition:
        if self.get(skill_id) is not None:
            raise SkillStoreError("skill_exists", "같은 id의 skill이 이미 있습니다.")
        skill = parse_skill_document(skill_id, document, source="user")
        self._write(skill_id, skill.document)
        return skill

    def update(self, skill_id: str, document: str) -> SkillDefinition:
        current = self.get(skill_id)
        if current is None:
            raise SkillStoreError("skill_not_found", "수정할 skill을 찾지 못했습니다.")
        if not current.editable:
            raise SkillStoreError("builtin_skill_readonly", "기본 skill은 직접 수정할 수 없습니다.")
        skill = parse_skill_document(skill_id, document, source="user")
        self._write(skill_id, skill.document)
        return skill

    def delete(self, skill_id: str) -> None:
        current = self.get(skill_id)
        if current is None:
            raise SkillStoreError("skill_not_found", "삭제할 skill을 찾지 못했습니다.")
        if not current.editable:
            raise SkillStoreError("builtin_skill_readonly", "기본 skill은 삭제할 수 없습니다.")
        skill_file = self.user_root / skill_id / "SKILL.md"
        skill_file.unlink(missing_ok=True)
        try:
            skill_file.parent.rmdir()
        except OSError:
            pass

    def _load_root(self, root: Path, *, source: str) -> list[SkillDefinition]:
        if not root.exists():
            return []
        loaded: list[SkillDefinition] = []
        for skill_file in sorted(root.glob("*/SKILL.md")):
            skill_id = skill_file.parent.name
            try:
                document = skill_file.read_text(encoding="utf-8")
                loaded.append(parse_skill_document(skill_id, document, source=source))
            except (OSError, UnicodeError, SkillStoreError) as exc:
                _logger.warning("SKILL.md 로드 실패: skill_id=%s error=%s", skill_id, type(exc).__name__)
        return loaded

    def _write(self, skill_id: str, document: str) -> None:
        target_dir = self.user_root / skill_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "SKILL.md"
        temporary = target.with_suffix(".md.tmp")
        temporary.write_text(document, encoding="utf-8")
        temporary.replace(target)


def format_tools_help(registry: dict[str, "ToolHandler"]) -> str:
    """현재 registry의 모든 tool 설명을 slash command 답변으로 만든다."""

    lines = ["사용 가능한 tools"]
    for category in ("smb", "database", "report", "skill"):
        handlers = [item for item in registry.values() if item.definition.category == category]
        if not handlers:
            continue
        lines.append(f"\n[{_CATEGORY_NAMES[category]}]")
        for handler in handlers:
            definition = handler.definition
            state = "사용 가능" if definition.enabled else "비활성"
            lines.append(f"- {definition.id} — {definition.display_name}: {definition.description} ({state})")
    lines.append("\n왼쪽 도구 분류에서 체크한 tool만 일반 채팅 중 agent가 호출할 수 있습니다.")
    return "\n".join(lines)


def format_skills_help(skills: tuple[SkillDefinition, ...]) -> str:
    """모든 SKILL.md 설명을 slash command 답변으로 만든다."""

    lines = ["사용 가능한 skills"]
    for skill in skills:
        source = "기본" if skill.source == "builtin" else "사용자"
        lines.append(f"- {skill.id} — {skill.description} ({source})")
    lines.append("\n채팅 입력창 아래 Skills 버튼에서 활성화하거나 새 SKILL.md를 추가·관리할 수 있습니다.")
    return "\n".join(lines)
