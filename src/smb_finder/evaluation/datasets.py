"""합성 문서 챗봇 JSONL dataset 로더."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import ValidationError

from .models import GoldenCase, GoldenDataset


class GoldenDatasetError(ValueError):
    """dataset 형식 또는 중복 case 오류."""


def load_golden_dataset(path: str | Path, *, version: str = "synthetic-smoke-v1") -> GoldenDataset:
    """JSONL을 완전히 검증하고 내용 hash와 함께 반환한다."""

    dataset_path = Path(path)
    try:
        raw = dataset_path.read_bytes()
    except OSError as exc:
        raise GoldenDatasetError(f"평가 dataset을 읽을 수 없습니다: {dataset_path}") from exc

    cases: list[GoldenCase] = []
    seen_ids: set[str] = set()
    for line_number, raw_line in enumerate(raw.decode("utf-8-sig").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            case = GoldenCase.model_validate(json.loads(line))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise GoldenDatasetError(f"평가 dataset {line_number}번째 줄이 유효하지 않습니다: {exc}") from exc
        if case.case_id in seen_ids:
            raise GoldenDatasetError(f"중복 case_id가 있습니다: {case.case_id}")
        seen_ids.add(case.case_id)
        cases.append(case)

    if not cases:
        raise GoldenDatasetError("평가 dataset에 case가 없습니다.")
    fingerprint = hashlib.sha256(raw).hexdigest()[:16]
    return GoldenDataset(name=dataset_path.name, version=version, fingerprint=fingerprint, cases=cases)
