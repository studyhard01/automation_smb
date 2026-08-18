"""합성 SOP 검색과 결정론적 QC 보고서 감사 서비스."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import re
import time

from .attachments import AttachmentStore
from .models import AuditFinding, AuditResult, ExtractedDocument, SopRule, SopSearchHit

_logger = logging.getLogger(__name__)
_TOKEN_RE = re.compile(r"[0-9a-zA-Z가-힣]+")
_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])[-+]?\d+(?:[.,]\d+)?(?![A-Za-z0-9])")


class QcAuditService:
    """로컬 첨부 추출, SOP 검색, 감사 판정을 한 프로세스 안에서 수행한다."""

    def __init__(self, attachment_store: AttachmentStore, rules_path: str | Path, *, budget_ms: int = 1000):
        self.attachment_store = attachment_store
        self.rules_path = Path(rules_path)
        self.budget_ms = budget_ms
        self._rules: tuple[SopRule, ...] | None = None

    def extract(self, attachment_id: str) -> ExtractedDocument:
        """첨부 문서의 텍스트를 로컬에서 추출한다."""

        return self.attachment_store.extract(attachment_id)

    def search_sop(self, query: str, limit: int = 5) -> tuple[list[SopSearchHit], float]:
        """합성 SOP를 코드·제목·별칭 토큰으로 검색한다."""

        started = time.perf_counter()
        normalized_query = self._normalize(query)
        query_tokens = set(_TOKEN_RE.findall(normalized_query))
        hits: list[SopSearchHit] = []
        for rule in self._load_rules():
            searchable = self._normalize(" ".join([rule.id, rule.title, *rule.aliases, rule.criterion]))
            score = 0.0
            if normalized_query and normalized_query in searchable:
                score += 8.0
            if self._normalize(rule.id) in normalized_query:
                score += 12.0
            score += float(sum(1 for token in query_tokens if len(token) > 1 and token in searchable))
            if score > 0 or not normalized_query:
                hits.append(SopSearchHit(rule=rule, score=score))
        hits.sort(key=lambda hit: (-hit.score, hit.rule.id))
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        _logger.info("Synthetic SOP search completed: hits=%d elapsed_ms=%.1f", len(hits[:limit]), elapsed_ms)
        return hits[: max(1, min(limit, 20))], elapsed_ms

    def audit(self, attachment_id: str) -> AuditResult:
        """첨부 문서를 추출하고 모든 합성 필수 규칙에 대해 감사한다."""

        started = time.perf_counter()
        document = self.extract(attachment_id)
        if document.status != "ok":
            reason = (
                "PDF 본문을 추출할 수 없습니다. 텍스트형 PDF인지 확인하세요. "
                "이미지형 PDF OCR은 1차 범위에 포함되지 않습니다."
                if document.attachment.extension == ".pdf"
                else "Markdown 본문을 추출할 수 없습니다. 파일 인코딩과 내용을 확인하세요."
            )
            finding = AuditFinding(
                rule_id="DOCUMENT-TEXT",
                title="문서 본문 추출",
                status="UNVERIFIABLE",
                criterion="감사 가능한 텍스트 본문이 있어야 함",
                evidence="local-extractor",
                reason=reason,
            )
            return self._result(document, [finding], sop_search_ms=0.0, audit_started=started)

        search_started = time.perf_counter()
        rules = self._load_rules()
        sop_search_ms = round((time.perf_counter() - search_started) * 1000, 1)
        audit_started = time.perf_counter()
        findings = [self._audit_rule(document.text, rule) for rule in rules]
        result = self._result(document, findings, sop_search_ms=sop_search_ms, audit_started=started)
        result.audit_ms = round((time.perf_counter() - audit_started) * 1000, 1)
        if result.elapsed_ms > self.budget_ms:
            _logger.warning(
                "QC audit exceeded budget: attachment_id=%s elapsed_ms=%.1f budget_ms=%d",
                attachment_id,
                result.elapsed_ms,
                self.budget_ms,
            )
        else:
            _logger.info(
                "QC audit completed: attachment_id=%s status=%s findings=%d elapsed_ms=%.1f",
                attachment_id,
                result.status,
                len(result.findings),
                result.elapsed_ms,
            )
        return result

    def format_audit(self, result: AuditResult) -> str:
        """감사 결과를 채팅에서 읽기 쉬운 한국어 텍스트로 만든다."""

        counts = result.counts
        lines = [
            f"QC 감사 결과: {result.status}",
            f"파일: {result.attachment.filename}",
            (
                "요약: "
                f"PASS {counts['PASS']} · WARNING {counts['WARNING']} · "
                f"FAIL {counts['FAIL']} · 확인 불가 {counts['UNVERIFIABLE']}"
            ),
            "",
        ]
        for finding in result.findings:
            observed = f" / 관찰값: {finding.observed_value}" if finding.observed_value else ""
            lines.extend(
                [
                    f"- [{finding.status}] {finding.rule_id} {finding.title}{observed}",
                    f"  판정: {finding.reason}",
                    f"  기준: {finding.criterion}",
                    f"  근거: {finding.evidence}",
                ]
            )
        lines.extend(
            [
                "",
                "주의: 현재 결과는 합성 SOP로 기능만 검증한 것이며 운영 QC 판정이 아닙니다.",
                (
                    f"처리 시간: 총 {result.elapsed_ms:.1f}ms "
                    f"(추출 {result.extraction_ms:.1f}ms, SOP {result.sop_search_ms:.1f}ms, "
                    f"판정 {result.audit_ms:.1f}ms)"
                ),
            ]
        )
        return "\n".join(lines)

    def _load_rules(self) -> tuple[SopRule, ...]:
        if self._rules is None:
            raw = json.loads(self.rules_path.read_text(encoding="utf-8"))
            self._rules = tuple(SopRule.model_validate(item) for item in raw.get("rules", []))
            if not self._rules:
                raise ValueError("합성 SOP 규칙이 비어 있습니다.")
        return self._rules

    def _audit_rule(self, text: str, rule: SopRule) -> AuditFinding:
        line = self._find_rule_line(text, rule)
        if not line:
            return AuditFinding(
                rule_id=rule.id,
                title=rule.title,
                status="UNVERIFIABLE",
                criterion=rule.criterion,
                evidence=rule.evidence,
                reason="보고서에서 해당 필수 항목을 찾지 못했습니다.",
            )
        if rule.value_type == "number":
            value = self._numeric_value(line, rule)
            if value is None:
                return AuditFinding(
                    rule_id=rule.id,
                    title=rule.title,
                    status="UNVERIFIABLE",
                    criterion=rule.criterion,
                    evidence=rule.evidence,
                    reason="항목은 찾았지만 숫자 측정값을 해석하지 못했습니다.",
                )
            observed = f"{value:g}{(' ' + rule.unit) if rule.unit else ''}"
            if (rule.minimum is not None and value < rule.minimum) or (
                rule.maximum is not None and value > rule.maximum
            ):
                status = "FAIL"
                reason = "측정값이 허용 범위를 벗어났습니다."
            elif self._near_boundary(value, rule):
                status = "WARNING"
                reason = "허용 범위 안이지만 경계값에 가깝습니다."
            else:
                status = "PASS"
                reason = "측정값이 허용 범위 안에 있습니다."
            return AuditFinding(
                rule_id=rule.id,
                title=rule.title,
                status=status,
                observed_value=observed,
                criterion=rule.criterion,
                evidence=rule.evidence,
                reason=reason,
            )

        normalized_line = self._normalize(line)
        accepted = next(
            (value for value in rule.accepted_values if self._normalize(value) in normalized_line),
            "",
        )
        return AuditFinding(
            rule_id=rule.id,
            title=rule.title,
            status="PASS" if accepted else "FAIL",
            observed_value=accepted or line.strip(" |")[:80],
            criterion=rule.criterion,
            evidence=rule.evidence,
            reason="허용 상태값을 확인했습니다." if accepted else "허용 상태값을 확인하지 못했습니다.",
        )

    def _find_rule_line(self, text: str, rule: SopRule) -> str:
        needles = [rule.id, rule.title, *rule.aliases]
        for line in text.splitlines():
            normalized_line = self._normalize(line)
            if any(self._normalize(needle) in normalized_line for needle in needles if needle.strip()):
                return line
        return ""

    def _numeric_value(self, line: str, rule: SopRule) -> float | None:
        scrubbed = line
        for label in [rule.id, rule.title, *rule.aliases]:
            scrubbed = re.sub(re.escape(label), " ", scrubbed, flags=re.IGNORECASE)
        for match in _NUMBER_RE.findall(scrubbed):
            try:
                return float(match.replace(",", "."))
            except ValueError:
                continue
        return None

    @staticmethod
    def _near_boundary(value: float, rule: SopRule) -> bool:
        if rule.warning_margin <= 0:
            return False
        return bool(
            (rule.minimum is not None and value <= rule.minimum + rule.warning_margin)
            or (rule.maximum is not None and value >= rule.maximum - rule.warning_margin)
        )

    def _result(
        self,
        document: ExtractedDocument,
        findings: list[AuditFinding],
        *,
        sop_search_ms: float,
        audit_started: float,
    ) -> AuditResult:
        counts = {status: sum(item.status == status for item in findings) for status in self._status_order()}
        if counts["FAIL"]:
            status = "FAIL"
        elif counts["UNVERIFIABLE"]:
            status = "UNVERIFIABLE"
        elif counts["WARNING"]:
            status = "WARNING"
        else:
            status = "PASS"
        return AuditResult(
            attachment=document.attachment,
            status=status,
            findings=findings,
            counts=counts,
            extraction_ms=document.elapsed_ms,
            sop_search_ms=sop_search_ms,
            elapsed_ms=round((time.perf_counter() - audit_started) * 1000, 1),
        )

    @staticmethod
    def _status_order() -> tuple[str, ...]:
        return ("PASS", "WARNING", "FAIL", "UNVERIFIABLE")

    @staticmethod
    def _normalize(value: str) -> str:
        return " ".join(_TOKEN_RE.findall(str(value or "").lower()))
