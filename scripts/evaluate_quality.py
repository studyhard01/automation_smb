"""automation_smb 저장소의 오프라인 품질 rubric 평가기."""

from __future__ import annotations

import argparse
import fnmatch
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUBRIC = REPO_ROOT / "config" / "project_quality_rubric.json"
_MARKDOWN_LINK = re.compile(r"!?\[[^\]]*]\((?P<target><[^>]+>|[^)\s]+)(?:\s+[\"'][^)]*[\"'])?\)")
_PRIVATE_IPV4 = re.compile(r"(?<![\d.])(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))(?:\.\d{1,3}){2,3}(?![\d.])")
_UNC = re.compile(r"\\\\(?P<host>[A-Za-z0-9._-]+)\\")
_SECRET_TOKENS = (
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[oprsu]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
_SECRET_NAME = r"(?:password|passwd|secret|api[_-]?key|access[_-]?token|admin[_-]?token)"
_SECRET_ASSIGNMENTS = (
    re.compile(rf"""(?im)\b{_SECRET_NAME}\b\s*=\s*["'](?P<value>[^"'#,\s}}\]]+)["']""", re.VERBOSE),
    re.compile(rf"""(?im)["']{_SECRET_NAME}["']\s*:\s*["'](?P<value>[^"'#,\s}}\]]+)["']""", re.VERBOSE),
    re.compile(
        r"""(?m)^\s*[A-Z][A-Z0-9_]*(?:PASSWORD|PASSWD|SECRET|API_KEY|ACCESS_TOKEN|ADMIN_TOKEN)
            [ \t]*=[ \t]*(?P<value>[^"'#,\s}\]]+)""",
        re.VERBOSE,
    ),
)
_MEDICAL_IDENTIFIERS = (
    re.compile(r"(?<!\d)\d{6}-[1-8]\d{6}(?!\d)"),
    re.compile(r"(?im)\b(?:mrn|patient[_ -]?id|환자번호)\s*[:=]\s*[\"']?[A-Za-z0-9-]{5,}"),
)


@dataclass(frozen=True)
class CommandResult:
    """외부 명령의 최소 결과."""

    returncode: int
    stdout: str = ""


@dataclass(frozen=True)
class CheckOutcome:
    """한 rubric criterion의 검사 결과."""

    passed: bool
    summary: str
    fraction: float = 1.0
    details: tuple[str, ...] = ()
    skipped: bool = False


@dataclass(frozen=True)
class CriterionResult:
    """점수가 반영된 criterion 결과."""

    id: str
    title: str
    category_id: str
    category_title: str
    points: float
    awarded: float
    hard_gate: bool
    status: str
    summary: str
    details: tuple[str, ...]


CommandRunner = Callable[[list[str], Path, dict[str, str]], CommandResult]


def load_rubric(path: Path = DEFAULT_RUBRIC) -> dict[str, Any]:
    """JSON rubric을 읽고 100점 구조를 검증한다."""

    rubric = json.loads(path.read_text(encoding="utf-8"))
    category_total = sum(float(category["weight"]) for category in rubric["categories"])
    if category_total != 100:
        raise ValueError(f"rubric category 합계가 100이 아닙니다: {category_total}")
    for category in rubric["categories"]:
        criterion_total = sum(float(item["points"]) for item in category["criteria"])
        if criterion_total != float(category["weight"]):
            raise ValueError(f"{category['id']} criterion 합계가 category weight와 다릅니다")
    if not 0 <= float(rubric["target_score"]) <= 100:
        raise ValueError("target_score는 0~100이어야 합니다")
    return rubric


def run_command(command: list[str], cwd: Path, env: dict[str, str]) -> CommandResult:
    """네트워크 없이 로컬 명령을 실행한다."""

    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return CommandResult(returncode=completed.returncode, stdout=completed.stdout)


def list_tracked_files(repo_root: Path) -> tuple[list[str], str | None]:
    """Git tracked 파일과 커밋 후보인 non-ignored 파일 목록을 반환한다."""

    completed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if completed.returncode != 0:
        return [], "Git tracked file 목록을 읽지 못했습니다."
    paths = [item.decode("utf-8", errors="replace") for item in completed.stdout.split(b"\0") if item]
    return paths, None


def _matches_glob(path: str, pattern: str) -> bool:
    normalized = path.replace("\\", "/")
    if fnmatch.fnmatch(normalized, pattern):
        return True
    if pattern.startswith("**/") and fnmatch.fnmatch(normalized, pattern[3:]):
        return True
    return False


def check_tracked_artifacts(
    repo_root: Path,
    policy: dict[str, Any],
    tracked_files: list[str] | None = None,
) -> CheckOutcome:
    """금지된 로컬 상태나 비밀 파일이 추적되는지 검사한다."""

    files, error = (tracked_files, None) if tracked_files is not None else list_tracked_files(repo_root)
    if error:
        return CheckOutcome(False, error)
    allowed = set(policy["allowed_tracked_paths"])
    forbidden = [
        path
        for path in files or []
        if path not in allowed and any(_matches_glob(path, pattern) for pattern in policy["forbidden_tracked_globs"])
    ]
    details = tuple(f"{path}: 금지 tracked artifact" for path in sorted(forbidden))
    return CheckOutcome(not forbidden, "금지 tracked artifact가 없습니다." if not forbidden else "금지 파일이 추적 중입니다.", details=details)


def _is_text_file(path: Path, extensions: set[str]) -> bool:
    return path.suffix.lower() in extensions or path.name in {".env", ".gitignore", ".gitattributes"}


def _safe_placeholder(value: str, file_path: str) -> bool:
    lowered = value.strip().lower()
    if (
        not lowered
        or lowered.startswith(("${", "<"))
        or "..." in lowered
        or any(token in lowered for token in ("dummy", "example", "synthetic", "test-token", "local-no-key"))
    ):
        return True
    return file_path.startswith("backend/tests/") and any(
        token in lowered for token in ("test", "local", "fake", "secret")
    )


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def check_sensitive_values(
    repo_root: Path,
    policy: dict[str, Any],
    tracked_files: list[str] | None = None,
) -> CheckOutcome:
    """민감 값 후보를 탐지하되 값 자체는 결과에 싣지 않는다."""

    files, error = (tracked_files, None) if tracked_files is not None else list_tracked_files(repo_root)
    if error:
        return CheckOutcome(False, error)
    extensions = set(policy["text_scan_extensions"])
    safe_ips = set(policy["safe_private_ip_literals"])
    safe_unc_hosts = set(policy["safe_unc_hosts"])
    findings: list[tuple[str, int, str]] = []
    for relative in files or []:
        path = repo_root / relative
        if not path.is_file() or not _is_text_file(path, extensions) or path.name == "uv.lock":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        for pattern in _SECRET_TOKENS:
            for match in pattern.finditer(text):
                findings.append((relative, _line_number(text, match.start()), "secret/token 형식"))
        for pattern in _SECRET_ASSIGNMENTS:
            for match in pattern.finditer(text):
                if not _safe_placeholder(match.group("value"), relative):
                    findings.append((relative, _line_number(text, match.start()), "비어 있지 않은 secret 설정"))
        for match in _PRIVATE_IPV4.finditer(text):
            candidate = match.group(0)
            try:
                address = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if str(address) not in safe_ips:
                findings.append((relative, _line_number(text, match.start()), "private network 주소"))
        for match in _UNC.finditer(text):
            if match.group("host").lower() not in safe_unc_hosts:
                findings.append((relative, _line_number(text, match.start()), "UNC host/path"))
        for pattern in _MEDICAL_IDENTIFIERS:
            for match in pattern.finditer(text):
                findings.append((relative, _line_number(text, match.start()), "의료/개인 식별자 형식"))
    unique = sorted(set(findings))
    details = tuple(f"{path}:{line}: {kind} 후보 1건(값 비표시)" for path, line, kind in unique)
    return CheckOutcome(
        not unique,
        "민감 값 후보가 없습니다." if not unique else f"민감 값 후보 {len(unique)}건을 확인해야 합니다.",
        details=details,
    )


def _required_tokens(repo_root: Path, file_tokens: dict[str, list[str]]) -> list[str]:
    errors: list[str] = []
    for relative, tokens in file_tokens.items():
        path = repo_root / relative
        if not path.is_file():
            errors.append(f"{relative}: 파일 없음")
            continue
        text = path.read_text(encoding="utf-8")
        missing = [token for token in tokens if token not in text]
        if missing:
            errors.append(f"{relative}: 필수 architecture marker {len(missing)}개 누락")
    return errors


def check_architecture_markers(repo_root: Path, policy: dict[str, Any], section: str, title: str) -> CheckOutcome:
    """파일별 필수 architecture marker를 검사한다."""

    errors = _required_tokens(repo_root, policy["architecture_markers"][section])
    return CheckOutcome(not errors, f"{title} marker가 유지됩니다." if not errors else f"{title} marker가 누락됐습니다.", details=tuple(errors))


def check_read_only_architecture(repo_root: Path, policy: dict[str, Any]) -> CheckOutcome:
    """DB read-only와 명시적으로 제한된 SMB 첨부 marker를 함께 검사한다."""

    errors = _required_tokens(repo_root, policy["architecture_markers"]["read_only"])
    source_root = repo_root / "backend" / "src" / "smb_finder"
    for path in source_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pattern in policy["forbidden_smb_mutation_patterns"]:
            if pattern in text:
                errors.append(f"{path.relative_to(repo_root).as_posix()}: SMB mutation marker 감지")
    return CheckOutcome(
        not errors,
        "DB 읽기 전용·제한 SMB 첨부 경계가 유지됩니다." if not errors else "저장소 접근 경계를 확인해야 합니다.",
        details=tuple(errors),
    )


def _fixture_error(path: str, line: int, message: str) -> str:
    return f"{path}:{line}: {message}"


def check_synthetic_fixtures(repo_root: Path, policy: dict[str, Any]) -> CheckOutcome:
    """Golden/Candidate JSONL의 합성·검토·근거 계약을 검사한다."""

    errors: list[str] = []
    seen_case_ids: set[str] = set()
    for contract in policy["fixture_contracts"]:
        relative = contract["path"]
        path = repo_root / relative
        if not path.is_file():
            errors.append(_fixture_error(relative, 0, "fixture 파일 없음"))
            continue
        case_count = 0
        for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not raw.strip():
                continue
            case_count += 1
            try:
                case = json.loads(raw)
            except json.JSONDecodeError:
                errors.append(_fixture_error(relative, line_number, "유효하지 않은 JSON"))
                continue
            case_id = case.get("case_id")
            inputs = case.get("inputs")
            expectations = case.get("expectations")
            tags = case.get("tags")
            provenance = case.get("provenance")
            if not isinstance(case_id, str) or not case_id.startswith("synthetic-"):
                errors.append(_fixture_error(relative, line_number, "case_id가 synthetic- 접두사가 아님"))
            elif case_id in seen_case_ids:
                errors.append(_fixture_error(relative, line_number, "case_id 중복"))
            else:
                seen_case_ids.add(case_id)
            if not isinstance(inputs, dict) or not isinstance(inputs.get("question"), str) or not inputs["question"].strip():
                errors.append(_fixture_error(relative, line_number, "inputs.question 누락"))
            if not isinstance(inputs, dict) or not isinstance(inputs.get("top_k"), int) or inputs["top_k"] < 1:
                errors.append(_fixture_error(relative, line_number, "inputs.top_k 계약 위반"))
            if not isinstance(expectations, dict) or not isinstance(expectations.get("should_answer"), bool):
                errors.append(_fixture_error(relative, line_number, "expectations.should_answer 계약 위반"))
            if not isinstance(tags, dict) or str(tags.get("synthetic", "")).lower() != "true":
                errors.append(_fixture_error(relative, line_number, "tags.synthetic=true 누락"))
            if not isinstance(provenance, dict) or provenance.get("review_status") != contract["review_status"]:
                errors.append(_fixture_error(relative, line_number, "review_status tier 불일치"))
            if contract["reviewed_on_required"] and not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}", str((provenance or {}).get("reviewed_on", ""))
            ):
                errors.append(_fixture_error(relative, line_number, "reviewed_on 누락/형식 오류"))
            review_decision = str((provenance or {}).get("review_decision", ""))
            reviewed_on = str((provenance or {}).get("reviewed_on", ""))
            if review_decision not in {"", "approve", "revise", "reject"}:
                errors.append(_fixture_error(relative, line_number, "review_decision 값 오류"))
            if contract["tier"] == "candidate" and bool(review_decision) != bool(
                re.fullmatch(r"\d{4}-\d{2}-\d{2}", reviewed_on)
            ):
                errors.append(_fixture_error(relative, line_number, "review_decision과 reviewed_on은 함께 기록해야 함"))
            document_keys = (expectations or {}).get("expected_document_keys", [])
            if not isinstance(document_keys, list) or any(
                not isinstance(key, str) or "/" in key or "\\" in key or Path(key).is_absolute()
                for key in document_keys
            ):
                errors.append(_fixture_error(relative, line_number, "document key는 파일명 기반이어야 함"))
            hashes = (provenance or {}).get("source_chunk_hashes", {})
            if not isinstance(hashes, dict) or any(
                not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None for value in hashes.values()
            ):
                errors.append(_fixture_error(relative, line_number, "source chunk SHA-256 계약 위반"))
        if case_count < int(contract["minimum_cases"]):
            errors.append(_fixture_error(relative, 0, f"case 수 부족({case_count}/{contract['minimum_cases']})"))
    return CheckOutcome(
        not errors,
        "합성 Golden/Candidate fixture 계약이 유효합니다." if not errors else "합성 fixture 계약 오류가 있습니다.",
        details=tuple(errors),
    )


def check_candidate_review_progress(repo_root: Path, policy: dict[str, Any]) -> CheckOutcome:
    """Candidate의 사람 검토 완료 비율을 advisory 점수로 환산한다."""

    contract = next(item for item in policy["fixture_contracts"] if item["tier"] == "candidate")
    path = repo_root / contract["path"]
    if not path.is_file():
        return CheckOutcome(False, "Candidate fixture가 없어 검토 진행률을 계산할 수 없습니다.", fraction=0.0)
    total = 0
    reviewed = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        total += 1
        case = json.loads(raw)
        provenance = case.get("provenance", {})
        if provenance.get("review_decision") in {"approve", "revise", "reject"} and provenance.get("reviewed_on"):
            reviewed += 1
    fraction = reviewed / total if total else 0.0
    return CheckOutcome(
        total > 0 and reviewed == total,
        f"Candidate 사람 검토: {reviewed}/{total}건",
        fraction=fraction,
    )


def check_runtime_conventions(repo_root: Path, policy: dict[str, Any]) -> CheckOutcome:
    """pyproject와 lock의 Python 개발 규약을 검사한다."""

    errors: list[str] = []
    pyproject_path = repo_root / "pyproject.toml"
    if not pyproject_path.is_file():
        return CheckOutcome(False, "pyproject.toml이 없습니다.")
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    project = data.get("project", {})
    runtime = policy["runtime"]
    if project.get("name") != runtime["project_name"]:
        errors.append("pyproject.toml: project.name 불일치")
    if project.get("requires-python") != runtime["python_requirement"]:
        errors.append("pyproject.toml: Python requirement 불일치")
    dependencies = [str(item).split("[", 1)[0].split("=", 1)[0].split("<", 1)[0].split(">", 1)[0] for item in project.get("dependencies", [])]
    for dependency in runtime["required_dependencies"]:
        if dependency not in dependencies:
            errors.append(f"pyproject.toml: 필수 dependency {dependency} 누락")
    ruff = data.get("tool", {}).get("ruff", {})
    if ruff.get("line-length") != runtime["ruff_line_length"]:
        errors.append("pyproject.toml: Ruff line-length 불일치")
    if ruff.get("src") != runtime["ruff_src"]:
        errors.append("pyproject.toml: Ruff src 설정 불일치")
    markers = data.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("markers", [])
    if not any(str(marker).split(":", 1)[0].strip() == runtime["pytest_marker"] for marker in markers):
        errors.append("pyproject.toml: integration pytest marker 누락")
    if not (repo_root / runtime["lock_file"]).is_file():
        errors.append(f"{runtime['lock_file']}: lock file 누락")
    return CheckOutcome(not errors, "Python 3.11·uv·Ruff·pytest 규약이 고정돼 있습니다." if not errors else "Python runtime 규약이 어긋났습니다.", details=tuple(errors))


def check_markdown_links(
    repo_root: Path,
    tracked_files: list[str] | None = None,
) -> CheckOutcome:
    """추적 중인 Markdown의 로컬 상대 링크 대상 존재 여부를 검사한다."""

    files, error = (tracked_files, None) if tracked_files is not None else list_tracked_files(repo_root)
    if error:
        return CheckOutcome(False, error)
    errors: list[str] = []
    for relative in files or []:
        if not relative.lower().endswith(".md"):
            continue
        path = repo_root / relative
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for match in _MARKDOWN_LINK.finditer(text):
            raw_target = match.group("target").strip("<>")
            if raw_target.startswith(("#", "http://", "https://", "mailto:", "data:")):
                continue
            target_without_anchor = unquote(raw_target.split("#", 1)[0].split("?", 1)[0])
            if not target_without_anchor:
                continue
            candidate = (path.parent / target_without_anchor).resolve()
            try:
                candidate.relative_to(repo_root.resolve())
            except ValueError:
                errors.append(f"{relative}:{_line_number(text, match.start())}: 저장소 밖 상대 링크")
                continue
            if not candidate.exists():
                errors.append(f"{relative}:{_line_number(text, match.start())}: 상대 링크 대상 없음")
    return CheckOutcome(not errors, "Markdown 상대 링크가 모두 유효합니다." if not errors else "깨진 Markdown 상대 링크가 있습니다.", details=tuple(errors))


def check_docs_alignment(repo_root: Path, policy: dict[str, Any]) -> CheckOutcome:
    """canonical 문서의 필수/금지 상태 주장을 검사한다."""

    errors: list[str] = []
    for relative, contract in policy["canonical_documents"].items():
        path = repo_root / relative
        if not path.is_file():
            errors.append(f"{relative}: canonical 문서 없음")
            continue
        text = path.read_text(encoding="utf-8")
        missing = [token for token in contract["required_tokens"] if token not in text]
        stale = [token for token in contract["forbidden_tokens"] if token in text]
        if missing:
            errors.append(f"{relative}: 현재 목표/구현 필수 주장 {len(missing)}개 누락")
        if stale:
            errors.append(f"{relative}: stale/상충 주장 {len(stale)}개 감지")
    return CheckOutcome(not errors, "canonical 문서가 목표·현행 구현과 정렬돼 있습니다." if not errors else "canonical 문서 정합성 오류가 있습니다.", details=tuple(errors))


def check_automation_wiring(repo_root: Path, policy: dict[str, Any]) -> CheckOutcome:
    """CI, 로컬 hook, push skill과 개발 교훈 검사가 연결됐는지 검사한다."""

    contract = policy["automation_contract"]
    command = contract["required_command"]
    errors: list[str] = []
    for key in (
        "workflow",
        "hook",
        "hook_enabler",
        "gitattributes",
        "lessons_document",
        "lessons_checker",
        "push_skill",
        "agent_guide",
    ):
        relative = contract[key]
        if not (repo_root / relative).is_file():
            errors.append(f"{relative}: 자동화 파일 누락")
    for key in ("workflow", "hook"):
        path = repo_root / contract[key]
        if path.is_file() and command not in path.read_text(encoding="utf-8"):
            errors.append(f"{contract[key]}: 공통 evaluator 명령 누락")
    enabler = repo_root / contract["hook_enabler"]
    if enabler.is_file() and "core.hooksPath .githooks" not in enabler.read_text(encoding="utf-8"):
        errors.append(f"{contract['hook_enabler']}: hooksPath 설정 누락")
    attributes = repo_root / contract["gitattributes"]
    if attributes.is_file() and ".githooks/* text eol=lf" not in attributes.read_text(encoding="utf-8"):
        errors.append(f"{contract['gitattributes']}: hook LF 고정 누락")
    hook = repo_root / contract["hook"]
    if hook.is_file() and contract["lessons_staged_command"] not in hook.read_text(encoding="utf-8"):
        errors.append(f"{contract['hook']}: staged 개발 교훈 검사 명령 누락")
    push_skill = repo_root / contract["push_skill"]
    if push_skill.is_file() and contract["lessons_push_command"] not in push_skill.read_text(encoding="utf-8"):
        errors.append(f"{contract['push_skill']}: outgoing diff 개발 교훈 검사 명령 누락")
    agent_guide = repo_root / contract["agent_guide"]
    if agent_guide.is_file() and contract["lessons_document"] not in agent_guide.read_text(encoding="utf-8"):
        errors.append(f"{contract['agent_guide']}: 작업 시작 시 개발 교훈 참조 누락")
    return CheckOutcome(
        not errors,
        "CI·pre-commit·push 절차가 품질 평가와 개발 교훈 검사에 연결돼 있습니다."
        if not errors
        else "품질 자동화 연결이 불완전합니다.",
        details=tuple(errors),
    )


def check_development_lessons(repo_root: Path, command_runner: CommandRunner) -> CheckOutcome:
    """전용 검사기로 간결한 개발 교훈 문서 계약을 검증한다."""

    checker = repo_root / "scripts" / "check_development_lessons.py"
    if not checker.is_file():
        return CheckOutcome(False, "개발 교훈 검사기가 없습니다.")
    environment = os.environ.copy()
    result = command_runner([sys.executable, str(checker)], repo_root, environment)
    if result.returncode == 0:
        summary = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "개발 교훈 문서가 유효합니다."
        return CheckOutcome(True, summary)
    return CheckOutcome(
        False,
        f"개발 교훈 문서 검사가 실패했습니다(return code {result.returncode}).",
        details=("명령: python scripts/check_development_lessons.py",),
    )


def build_quality_commands(repo_root: Path, basetemp: Path, python_executable: str | None = None) -> dict[str, list[str]]:
    """Ruff와 비통합 pytest 명령을 한 곳에서 정의한다."""

    python = python_executable or sys.executable
    return {
        "ruff": [
            python,
            "-m",
            "ruff",
            "check",
            "backend/src/smb_finder/api.py",
            "backend/src/smb_finder/config.py",
            "backend/src/smb_finder/intent.py",
            "backend/src/smb_finder/models.py",
            "backend/src/smb_finder/llmops_search.py",
            "backend/src/smb_finder/llmops_multistore_search.py",
            "backend/src/smb_finder/llmops_retrieval.py",
            "backend/src/smb_finder/llmops_artifacts.py",
            "backend/src/smb_finder/llmops_graph.py",
            "backend/src/smb_finder/playground/document_api.py",
            "backend/src/smb_finder/playground/document_chat.py",
            "backend/src/smb_finder/playground/document_models.py",
            "backend/src/smb_finder/playground/proposal_context.py",
            "backend/src/smb_finder/playground/proposal_draft.py",
            "backend/src/smb_finder/playground/proposal_evidence.py",
            "backend/src/smb_finder/playground/upload_api.py",
            "backend/src/smb_finder/playground/upload_models.py",
            "backend/src/smb_finder/playground/upload_service.py",
            "backend/src/smb_finder/evaluation/proposal_live_runner.py",
            "backend/src/smb_finder/evaluation/proposal_models.py",
            "backend/src/smb_finder/evaluation/proposal_runner.py",
            "backend/src/smb_finder/evaluation/proposal_scoring.py",
            "backend/tests/test_api_contracts.py",
            "backend/tests/test_container_packaging.py",
            "backend/tests/test_development_lessons.py",
            "backend/tests/test_frontend_build.py",
            "backend/tests/test_llmops_api_contracts.py",
            "backend/tests/test_llmops_retrieval.py",
            "backend/tests/test_llmops_search.py",
            "backend/tests/test_llmops_stores.py",
            "backend/tests/test_project_quality.py",
            "backend/tests/test_proposal_draft.py",
            "backend/tests/test_proposal_evaluation.py",
            "backend/tests/test_proposal_live_runner.py",
            "backend/tests/test_upload_api.py",
            "scripts/check_development_lessons.py",
            "scripts/evaluate_quality.py",
            "scripts/run_proposal_evaluation.py",
        ],
        "pytest": [
            python,
            "-m",
            "pytest",
            "backend/tests/test_api_contracts.py",
            "backend/tests/test_container_packaging.py",
            "backend/tests/test_development_lessons.py",
            "backend/tests/test_frontend_build.py",
            "backend/tests/test_llmops_api_contracts.py",
            "backend/tests/test_llmops_retrieval.py",
            "backend/tests/test_llmops_search.py",
            "backend/tests/test_llmops_stores.py",
            "backend/tests/test_project_quality.py",
            "backend/tests/test_proposal_draft.py",
            "backend/tests/test_proposal_evaluation.py",
            "backend/tests/test_proposal_live_runner.py",
            "backend/tests/test_upload_api.py",
            "-m",
            "not integration",
            "-p",
            "no:cacheprovider",
            f"--basetemp={basetemp}",
        ],
    }


def _create_quality_run_root(repo_root: Path) -> Path:
    """live 평가 원시 산출물 보호 규칙과 충돌하지 않는 저장소 밖 임시 루트를 만든다."""

    run_root = Path(tempfile.mkdtemp(prefix="automation-smb-quality-")).resolve()
    resolved_repo = repo_root.resolve()
    if run_root == resolved_repo or run_root.is_relative_to(resolved_repo):
        shutil.rmtree(run_root, ignore_errors=True)
        raise RuntimeError("quality_temp_root_inside_repository")
    return run_root


def _remove_quality_run_root(run_root: Path) -> None:
    """평가 중 생성한 pytest·live runner 임시 산출물을 실행 결과와 관계없이 제거한다."""

    try:
        shutil.rmtree(run_root)
    except OSError as exc:
        raise RuntimeError("quality_temp_cleanup_failed") from exc


def _run_quality_command(
    name: str,
    repo_root: Path,
    basetemp: Path,
    command_runner: CommandRunner,
) -> CheckOutcome:
    commands = build_quality_commands(repo_root, basetemp)
    environment = os.environ.copy()
    environment.update({"TMP": str(basetemp.parent), "TEMP": str(basetemp.parent), "TMPDIR": str(basetemp.parent)})
    result = command_runner(commands[name], repo_root, environment)
    label = "Ruff" if name == "ruff" else "비통합 pytest"
    command_text = " ".join(commands[name][1:])
    if result.returncode == 0:
        return CheckOutcome(True, f"{label}가 통과했습니다.", details=(f"명령: python {command_text}",))
    return CheckOutcome(
        False,
        f"{label}가 실패했습니다(return code {result.returncode}).",
        details=(f"명령: python {command_text}", "상세 실패값은 출력하지 않습니다. 위 명령을 직접 실행해 확인하세요."),
    )


def _metric_fraction(metric: dict[str, Any]) -> float:
    """목표 충족 여부를 이진 점수로 반환한다."""

    value = float(metric["value"])
    target = float(metric["target"])
    if metric["direction"] == "higher":
        return 1.0 if value >= target else 0.0
    if metric["direction"] == "lower":
        return 1.0 if value <= target else 0.0
    raise ValueError(f"지원하지 않는 metric direction입니다: {metric['direction']}")


def _evidence_integrity(repo_root: Path, rubric: dict[str, Any]) -> tuple[bool, int, list[str]]:
    evidence = rubric["measured_evidence"]
    errors: list[str] = []
    source = repo_root / evidence["source"]
    if not source.is_file():
        errors.append("실측 evidence source 문서가 없습니다.")
    else:
        text = source.read_text(encoding="utf-8")
        if evidence["run_fingerprint"] not in text:
            errors.append("실측 run fingerprint가 source 문서와 일치하지 않습니다.")
        required_values = tuple(str(metric["value"]) for metric in evidence["metrics"].values())
        if any(value not in text for value in required_values):
            errors.append("실측 metric이 source 문서와 정렬되지 않았습니다.")
    if re.fullmatch(r"[0-9a-f]{16}", evidence["dataset_fingerprint"]) is None:
        errors.append("dataset fingerprint 형식이 유효하지 않습니다.")
    try:
        age_days = (date.today() - date.fromisoformat(evidence["recorded_on"])).days
    except ValueError:
        errors.append("실측 recorded_on 형식이 유효하지 않습니다.")
        age_days = int(evidence["max_age_days"]) + 1
    if age_days < 0:
        errors.append("실측 recorded_on이 미래 날짜입니다.")
    return not errors, age_days, errors


def check_evidence_freshness(repo_root: Path, rubric: dict[str, Any]) -> CheckOutcome:
    """실측의 provenance와 최대 유효 기간을 검사한다."""

    if not rubric["measured_evidence"].get("enabled", True):
        return CheckOutcome(True, "현재 수직 흐름의 합성 실측 baseline이 아직 없습니다.", fraction=0.0, skipped=True)

    valid, age_days, errors = _evidence_integrity(repo_root, rubric)
    max_age = int(rubric["measured_evidence"]["max_age_days"])
    fresh = valid and age_days <= max_age
    if not valid:
        return CheckOutcome(False, "합성 RAG 실측 provenance가 유효하지 않습니다.", fraction=0.0, details=tuple(errors))
    if not fresh:
        return CheckOutcome(False, f"합성 RAG 실측이 오래됐습니다({age_days}일, 기준 {max_age}일).", fraction=0.0)
    return CheckOutcome(True, f"합성 RAG 실측 provenance가 유효하고 최신입니다({age_days}일).")


def check_latency_evidence(repo_root: Path, rubric: dict[str, Any], metric_name: str, label: str) -> CheckOutcome:
    """낮을수록 좋은 지연 실측을 advisory 점수로 환산한다."""

    if not rubric["measured_evidence"].get("enabled", True):
        return CheckOutcome(True, f"{label} baseline이 아직 없습니다.", fraction=0.0, skipped=True)

    valid, _age_days, errors = _evidence_integrity(repo_root, rubric)
    if not valid:
        return CheckOutcome(False, f"{label} evidence가 유효하지 않습니다.", fraction=0.0, details=tuple(errors))
    metric = rubric["measured_evidence"]["metrics"][metric_name]
    fraction = _metric_fraction(metric)
    passed = fraction >= 1.0
    return CheckOutcome(
        passed,
        f"{label}: {float(metric['value']):.1f}ms / 목표 {float(metric['target']):.1f}ms",
        fraction=fraction,
    )


def check_rag_quality_evidence(repo_root: Path, rubric: dict[str, Any]) -> CheckOutcome:
    """Hit@5·no-answer·groundedness 실측을 advisory 점수로 환산한다."""

    if not rubric["measured_evidence"].get("enabled", True):
        return CheckOutcome(True, "현재 수직 흐름의 RAG 품질 baseline이 아직 없습니다.", fraction=0.0, skipped=True)

    valid, _age_days, errors = _evidence_integrity(repo_root, rubric)
    if not valid:
        return CheckOutcome(False, "합성 RAG 품질 evidence가 유효하지 않습니다.", fraction=0.0, details=tuple(errors))
    metrics = rubric["measured_evidence"]["metrics"]
    selected = [metrics["retrieval_hit_at_5_pct"], metrics["no_answer_accuracy_pct"], metrics["groundedness_pct"]]
    fraction = sum(_metric_fraction(metric) for metric in selected) / len(selected)
    summary = (
        f"Hit@5 {selected[0]['value']:.1f}% / no-answer {selected[1]['value']:.1f}% / "
        f"groundedness {selected[2]['value']:.1f}%"
    )
    return CheckOutcome(fraction >= 1.0, summary, fraction=fraction)


def _status(outcome: CheckOutcome) -> str:
    if outcome.skipped:
        return "skipped"
    if outcome.passed and outcome.fraction >= 1.0:
        return "passed"
    return "failed" if outcome.passed is False else "warning"


def _result_from_outcome(category: dict[str, Any], criterion: dict[str, Any], outcome: CheckOutcome) -> CriterionResult:
    fraction = max(0.0, min(1.0, float(outcome.fraction)))
    if criterion["hard_gate"] and not outcome.passed:
        fraction = 0.0
    awarded = 0.0 if outcome.skipped else round(float(criterion["points"]) * fraction, 2)
    status = _status(outcome)
    if not criterion["hard_gate"] and not outcome.skipped and (not outcome.passed or fraction < 1.0):
        status = "advisory"
    return CriterionResult(
        id=criterion["id"],
        title=criterion["title"],
        category_id=category["id"],
        category_title=category["title"],
        points=float(criterion["points"]),
        awarded=awarded,
        hard_gate=bool(criterion["hard_gate"]),
        status=status,
        summary=outcome.summary,
        details=outcome.details,
    )


def evaluate_project(
    repo_root: Path,
    rubric: dict[str, Any],
    *,
    static_only: bool = False,
    command_runner: CommandRunner = run_command,
    tracked_files: list[str] | None = None,
) -> list[CriterionResult]:
    """rubric의 모든 criterion을 평가한다."""

    policy = rubric["policy"]
    run_root = None if static_only else _create_quality_run_root(repo_root)
    basetemp = None if run_root is None else run_root / "pytest"
    static_checks: dict[str, Callable[[], CheckOutcome]] = {
        "tracked_artifacts": lambda: check_tracked_artifacts(repo_root, policy, tracked_files),
        "sensitive_values": lambda: check_sensitive_values(repo_root, policy, tracked_files),
        "read_only_architecture": lambda: check_read_only_architecture(repo_root, policy),
        "synthetic_fixtures": lambda: check_synthetic_fixtures(repo_root, policy),
        "candidate_review_progress": lambda: check_candidate_review_progress(repo_root, policy),
        "index_first_architecture": lambda: check_architecture_markers(repo_root, policy, "index_first", "인덱스 우선"),
        "timeout_elapsed_architecture": lambda: check_architecture_markers(
            repo_root, policy, "timeout_elapsed", "timeout·elapsed"
        ),
        "evidence_freshness": lambda: check_evidence_freshness(repo_root, rubric),
        "retrieval_latency_evidence": lambda: check_latency_evidence(
            repo_root, rubric, "retrieval_p95_ms", "retrieval p95"
        ),
        "end_to_end_latency_evidence": lambda: check_latency_evidence(
            repo_root, rubric, "end_to_end_p95_ms", "end-to-end p95"
        ),
        "runtime_conventions": lambda: check_runtime_conventions(repo_root, policy),
        "api_tool_contracts": lambda: check_architecture_markers(repo_root, policy, "api_tool_contracts", "API·tool 계약"),
        "rag_quality_evidence": lambda: check_rag_quality_evidence(repo_root, rubric),
        "markdown_links": lambda: check_markdown_links(repo_root, tracked_files),
        "docs_alignment": lambda: check_docs_alignment(repo_root, policy),
        "automation_wiring": lambda: check_automation_wiring(repo_root, policy),
        "development_lessons": lambda: check_development_lessons(repo_root, command_runner),
        "observability_architecture": lambda: check_architecture_markers(
            repo_root, policy, "observability", "관측성"
        ),
    }
    results: list[CriterionResult] = []
    try:
        for category in rubric["categories"]:
            for criterion in category["criteria"]:
                check_name = criterion["check"]
                if check_name in {"ruff", "pytest"}:
                    if static_only:
                        outcome = CheckOutcome(
                            True,
                            "정적 전용 모드에서 실행 검사를 건너뛰었습니다.",
                            fraction=0.0,
                            skipped=True,
                        )
                    else:
                        if basetemp is None:
                            raise RuntimeError("quality_temp_root_unavailable")
                        outcome = _run_quality_command(check_name, repo_root, basetemp, command_runner)
                else:
                    outcome = static_checks[check_name]()
                results.append(_result_from_outcome(category, criterion, outcome))
    finally:
        if run_root is not None:
            _remove_quality_run_root(run_root)
    return results


def summarize_results(rubric: dict[str, Any], results: list[CriterionResult]) -> dict[str, Any]:
    """criterion 결과를 category와 총점으로 집계한다."""

    categories: list[dict[str, Any]] = []
    for category in rubric["categories"]:
        selected = [result for result in results if result.category_id == category["id"]]
        categories.append(
            {
                "id": category["id"],
                "title": category["title"],
                "score": round(sum(result.awarded for result in selected), 2),
                "max_score": float(category["weight"]),
            }
        )
    hard_failures = [result.id for result in results if result.hard_gate and result.status == "failed"]
    skipped_hard_gates = [result.id for result in results if result.hard_gate and result.status == "skipped"]
    score = round(sum(result.awarded for result in results), 2)
    return {
        "rubric_id": rubric["rubric_id"],
        "rubric_version": rubric["version"],
        "evaluated_on": date.today().isoformat(),
        "score": score,
        "max_score": 100.0,
        "target_score": float(rubric["target_score"]),
        "target_met": score >= float(rubric["target_score"]),
        "hard_gates_passed": not hard_failures and not skipped_hard_gates,
        "hard_gate_failures": hard_failures,
        "skipped_hard_gates": skipped_hard_gates,
        "categories": categories,
        "criteria": [asdict(result) for result in results],
    }


def determine_exit_code(report: dict[str, Any], *, strict_score: bool) -> int:
    """기본은 hard gate, strict는 목표 점수까지 exit code에 반영한다."""

    if not report["hard_gates_passed"]:
        return 1
    if strict_score and not report["target_met"]:
        return 2
    return 0


def print_korean_summary(report: dict[str, Any], *, static_only: bool) -> None:
    """사람이 빠르게 읽을 수 있는 한국어 요약을 출력한다."""

    mode = "정적 전용" if static_only else "전체 오프라인"
    print(f"automation_smb 프로젝트 품질 평가 ({mode}, rubric v{report['rubric_version']})")
    print(f"총점: {report['score']:.2f}/100 (목표 {report['target_score']:.0f})")
    for category in report["categories"]:
        print(f"- {category['title']}: {category['score']:.2f}/{category['max_score']:.0f}")
    if report["hard_gates_passed"]:
        hard_status = "통과"
    elif report["hard_gate_failures"]:
        hard_status = f"실패 {len(report['hard_gate_failures'])}건"
    else:
        hard_status = f"미확정(실행 생략 {len(report['skipped_hard_gates'])}건)"
    print(f"하드 게이트: {hard_status}")
    if report["skipped_hard_gates"]:
        print(f"실행 생략 하드 게이트: {', '.join(report['skipped_hard_gates'])}")
    issues = [item for item in report["criteria"] if item["status"] in {"failed", "advisory"}]
    if issues:
        print("감점·조치 항목:")
        for item in issues:
            kind = "하드 게이트" if item["hard_gate"] else "권고"
            print(f"- [{kind}] {item['title']}: {item['summary']} ({item['awarded']:.2f}/{item['points']:.0f})")
            for detail in item["details"]:
                print(f"  - {detail}")
    else:
        print("감점·조치 항목: 없음")


def _write_json_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="automation_smb 저장소 전용 100점 품질 평가기")
    parser.add_argument("--rubric", type=Path, default=DEFAULT_RUBRIC, help="rubric JSON 경로")
    parser.add_argument("--json-output", type=Path, help="machine-readable JSON 결과 저장 경로")
    parser.add_argument("--strict-score", action="store_true", help="하드 게이트 외에 85점 목표도 exit code로 강제")
    parser.add_argument("--static-only", action="store_true", help="Ruff와 pytest를 실행하지 않고 정적 계약만 검사")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        rubric = load_rubric(args.rubric.resolve())
        results = evaluate_project(REPO_ROOT, rubric, static_only=args.static_only)
        report = summarize_results(rubric, results)
    except (OSError, ValueError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        print(f"품질 평가기 구성 오류: {type(exc).__name__}", file=sys.stderr)
        return 3
    print_korean_summary(report, static_only=args.static_only)
    if args.json_output:
        _write_json_report(args.json_output.resolve(), report)
        print(f"JSON 결과: {args.json_output}")
    return determine_exit_code(report, strict_score=args.strict_score)


if __name__ == "__main__":
    raise SystemExit(main())
