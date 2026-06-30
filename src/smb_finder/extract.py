"""파일 본문 추출 — 바이트 → 검색용 텍스트.

내용 검색의 첫 단계이자 가장 깨지기 쉬운 부분이다. 확장자별 추출기를 레지스트리로 두고,
지원하지 않거나 추출에 실패한 파일은 **조용히 건너뛰지 않고** 상태(status)로 남긴다.

설계 원칙:
- **외부 의존성 최소화.** 텍스트 계열은 stdlib 디코드, docx/xlsx는 `zipfile`+정규식(설치 불필요).
  pdf만 선택 의존성(`pypdf`)이며 미설치 시 unsupported로 떨어진다(서비스는 계속 동작).
- **한국어 인코딩.** 진단검사실 파일은 cp949(euc-kr)가 흔하다 → utf-8 실패 시 cp949로 폴백.
- **지연/메모리 보호.** 파일당 저장 텍스트를 max_chars로 잘라 인덱스 비대화를 막는다.
- **보안.** 추출 텍스트(환자 내용)는 절대 외부로 보내지 않는다 — 로컬 인덱스에만 적재된다.
"""

from __future__ import annotations

import html
import io
import logging
import re
import zipfile
from dataclasses import dataclass
from typing import Callable

_logger = logging.getLogger(__name__)

# 텍스트로 그대로 디코드하는 확장자 (stdlib만 사용)
_TEXT_EXT = {".txt", ".csv", ".tsv", ".md", ".log", ".json", ".xml", ".htm", ".html", ".yaml", ".yml"}
# 디코드 시도 순서 — utf-8 우선, 한국어 cp949 폴백, 최후엔 손실 허용
_ENCODINGS = ("utf-8-sig", "utf-8", "cp949")

_TAG = re.compile(r"<[^>]+>")
_WT = re.compile(r"<w:t[^>]*>(.*?)</w:t>", re.S)          # docx 텍스트 런
_PARA = re.compile(r"</w:p>")                              # docx 문단 경계
_XT = re.compile(r"<t[^>]*>(.*?)</t>", re.S)               # xlsx 공유문자열/인라인 텍스트
_AT = re.compile(r"<a:t>(.*?)</a:t>", re.S)               # pptx(DrawingML) 텍스트 런


@dataclass
class ExtractResult:
    """추출 결과 1건."""

    text: str        # 검색에 넣을 본문 (실패/미지원이면 "")
    status: str      # "ok" | "empty" | "unsupported" | "error"
    detail: str = ""  # 진단용 메모 (예: 예외 메시지). 환자 내용은 담지 않는다.


def _decode(data: bytes) -> str:
    """바이트를 텍스트로 디코드한다 (utf-8 → cp949 → 손실 허용)."""
    for enc in _ENCODINGS:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _strip_html(text: str) -> str:
    """HTML/XML 태그를 제거하고 엔티티를 푼다."""
    return html.unescape(_TAG.sub(" ", text))


def _extract_text_like(data: bytes) -> str:
    """txt/csv/json/xml/html 등 — 디코드 후 (html류면) 태그 제거."""
    return _decode(data)


def _extract_html(data: bytes) -> str:
    """html/htm — 태그 제거."""
    return _strip_html(_decode(data))


def _extract_docx(data: bytes) -> str:
    """docx — zip 내부 word/document.xml에서 <w:t> 텍스트를 문단 단위로 모은다 (의존성 없음)."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        if "word/document.xml" not in z.namelist():
            return ""
        xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    # 문단 경계를 줄바꿈으로 표시한 뒤 런 텍스트를 이어붙인다
    xml = _PARA.sub("\n", xml)
    parts = [html.unescape(m) for m in _WT.findall(xml)]
    return re.sub(r"[ \t]+", " ", "".join(parts))


def _extract_xlsx(data: bytes) -> str:
    """xlsx — 공유문자열 + 시트 인라인 텍스트를 모은다 (의존성 없음).

    숫자 셀은 검색 가치가 낮아 생략하고, 텍스트(<t>)만 모은다.
    """
    texts: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        if "xl/sharedStrings.xml" in names:
            xml = z.read("xl/sharedStrings.xml").decode("utf-8", errors="replace")
            texts.extend(_XT.findall(xml))
        for n in names:
            if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"):
                xml = z.read(n).decode("utf-8", errors="replace")
                texts.extend(_XT.findall(xml))
    return " ".join(html.unescape(t) for t in texts)


def _extract_pptx(data: bytes) -> str:
    """pptx — 각 슬라이드 xml(ppt/slides/slideN.xml)의 <a:t> 텍스트를 모은다 (의존성 없음).

    슬라이드 노트(ppt/notesSlides)도 포함해 검색 누락을 줄인다. 슬라이드 단위로 줄바꿈.
    """
    texts: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        # slide1, slide2 ... 순서대로 정렬해 본문 순서를 보존
        slides = sorted(
            n for n in z.namelist()
            if (n.startswith("ppt/slides/slide") or n.startswith("ppt/notesSlides/notesSlide"))
            and n.endswith(".xml")
        )
        for n in slides:
            xml = z.read(n).decode("utf-8", errors="replace")
            runs = [html.unescape(m) for m in _AT.findall(xml)]
            if runs:
                texts.append(" ".join(runs))
    return "\n".join(texts)


def _extract_pdf(data: bytes) -> str:
    """pdf — pypdf가 설치돼 있으면 텍스트 추출, 없으면 빈 문자열(→ unsupported)."""
    try:
        from pypdf import PdfReader  # 선택 의존성
    except ImportError:
        return ""
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


# 확장자 → 추출 함수
_EXTRACTORS: dict[str, Callable[[bytes], str]] = {
    **{ext: _extract_text_like for ext in _TEXT_EXT},
    ".htm": _extract_html,
    ".html": _extract_html,
    ".docx": _extract_docx,
    ".xlsx": _extract_xlsx,
    ".pptx": _extract_pptx,
    ".pdf": _extract_pdf,
}

# 내용 검색이 지원하는 확장자 (인덱서가 walk 필터로도 사용)
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(_EXTRACTORS)


def extract_text(name: str, data: bytes, max_chars: int = 200_000) -> ExtractResult:
    """파일명/바이트에서 검색용 텍스트를 추출한다.

    Args:
        name: 파일명 (확장자 판별용).
        data: 파일 바이트.
        max_chars: 저장할 텍스트 상한 (인덱스 비대화·지연 방지). 초과분은 자른다.

    Returns:
        ExtractResult — 미지원/실패도 status로 구분해 돌려준다(예외 던지지 않음).
    """
    ext = _ext_of(name)
    extractor = _EXTRACTORS.get(ext)
    if extractor is None:
        return ExtractResult(text="", status="unsupported", detail=f"ext={ext or '없음'}")
    try:
        text = extractor(data)
    except Exception as e:  # noqa: BLE001 — 한 파일 실패가 전체 빌드를 멈추지 않게
        _logger.debug("추출 실패 %s: %s", name, e)
        return ExtractResult(text="", status="error", detail=type(e).__name__)

    text = text.strip()
    if not text:
        # 추출기는 있으나 본문이 비었음 (예: pypdf 미설치, 빈 문서, 이미지 PDF)
        return ExtractResult(text="", status="empty")
    if len(text) > max_chars:
        text = text[:max_chars]
    return ExtractResult(text=text, status="ok")


def _ext_of(name: str) -> str:
    """파일명에서 소문자 확장자(점 포함)를 뽑는다. 없으면 빈 문자열."""
    dot = name.rfind(".")
    return name[dot:].lower() if dot != -1 else ""
