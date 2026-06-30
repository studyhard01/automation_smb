"""내용 검색 슬라이스 검증 — 라이브 SMB 없이 추출·FTS5·검색을 확인한다.

추출(extract)과 FTS5 저장/검색(content_index)은 순수 로직이라 SMB 없이 단위 테스트한다.
한국어 cp949 인코딩과 trigram 부분일치, 짧은 질의 LIKE 폴백을 함께 확인한다.
"""

from __future__ import annotations

import io
import zipfile

from smb_finder.config import Settings
from smb_finder.content_index import ContentIndex
from smb_finder.content_search import ContentSearcher
from smb_finder.extract import extract_text
from smb_finder.models import ContentSearchRequest


# ── 추출 ────────────────────────────────────────────────────
def test_extract_text_utf8():
    res = extract_text("note.txt", "환자 검체 BRCA1 변이".encode("utf-8"))
    assert res.status == "ok"
    assert "BRCA1" in res.text


def test_extract_text_cp949_korean():
    # 진단검사실 파일에 흔한 cp949(euc-kr) 인코딩도 깨지지 않아야 한다
    res = extract_text("legacy.csv", "유전자검사 결과".encode("cp949"))
    assert res.status == "ok"
    assert "유전자검사" in res.text


def test_extract_unsupported_ext():
    res = extract_text("scan.hwp", b"\xd0\xcf\x11\xe0")  # OLE 시그니처, 미지원
    assert res.status == "unsupported"
    assert res.text == ""


def test_extract_docx_from_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w:p><w:t>음성 판정 보고서</w:t></w:p>")
    res = extract_text("report.docx", buf.getvalue())
    assert res.status == "ok"
    assert "음성 판정 보고서" in res.text


def test_extract_xlsx_shared_strings():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/sharedStrings.xml", "<sst><si><t>염기서열 분석</t></si></sst>")
        z.writestr("xl/worksheets/sheet1.xml", "<worksheet/>")
    res = extract_text("data.xlsx", buf.getvalue())
    assert res.status == "ok"
    assert "염기서열 분석" in res.text


def test_extract_pptx_slides():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("ppt/slides/slide1.xml", "<p:sld><a:t>유전자 패널 검사</a:t><a:t>결과 요약</a:t></p:sld>")
        z.writestr("ppt/slides/slide2.xml", "<p:sld><a:t>BRCA1 변이 음성</a:t></p:sld>")
    res = extract_text("deck.pptx", buf.getvalue())
    assert res.status == "ok"
    assert "유전자 패널 검사" in res.text
    assert "BRCA1 변이 음성" in res.text


def test_extract_truncates_to_max_chars():
    res = extract_text("big.txt", ("가" * 1000).encode("utf-8"), max_chars=100)
    assert len(res.text) == 100


# ── FTS5 저장/검색 ──────────────────────────────────────────
def _index(tmp_path) -> ContentIndex:
    idx = ContentIndex(str(tmp_path / "content.fts.db"))
    idx.begin_rebuild()
    idx.add(path="검사/a.txt", name="a.txt", content="환자 검체 BRCA1 변이 보고서",
            ext=".txt", size=100, mtime=1.0)
    idx.add(path="검사/b.docx", name="b.docx", content="염기서열 분석 음성 판정",
            ext=".docx", size=200, mtime=2.0)
    idx.commit()
    return idx


def test_search_english_token(tmp_path):
    idx = _index(tmp_path)
    hits = idx.search(["BRCA1"], limit=10)
    assert [h.name for h in hits] == ["a.txt"]
    assert "BRCA1" in hits[0].snippet
    idx.close()


def test_search_korean_substring_trigram(tmp_path):
    idx = _index(tmp_path)
    # 3글자 이상 한국어 부분일치 (trigram)
    hits = idx.search(["염기서열"], limit=10)
    assert [h.name for h in hits] == ["b.docx"]
    idx.close()


def test_search_short_query_like_fallback(tmp_path):
    idx = _index(tmp_path)
    # 2글자 질의는 trigram이 못 잡으므로 LIKE 폴백으로라도 찾아야 한다
    hits = idx.search(["검체"], limit=10)
    assert any(h.name == "a.txt" for h in hits)
    idx.close()


def test_search_no_match(tmp_path):
    idx = _index(tmp_path)
    assert idx.search(["존재하지않는키워드"], limit=10) == []
    idx.close()


def test_clear_subpath_scoped(tmp_path):
    # 폴더 단위로만 비워야 한다 — 다른 폴더 인덱스는 보존
    idx = ContentIndex(str(tmp_path / "content.fts.db"))
    idx.add(path="A/2026/x.txt", name="x.txt", content="BRCA1 변이", ext=".txt", size=1, mtime=1.0)
    idx.add(path="A/2025/y.txt", name="y.txt", content="BRCA1 변이", ext=".txt", size=1, mtime=1.0)
    idx.add(path="B/z.txt", name="z.txt", content="BRCA1 변이", ext=".txt", size=1, mtime=1.0)
    idx.commit()
    assert idx.count() == 3

    idx.clear_subpath("A/2026")  # 이 폴더 아래만 삭제
    idx.commit()
    names = {h.name for h in idx.search(["BRCA1"], limit=10)}
    assert names == {"y.txt", "z.txt"}  # A/2026/x.txt만 사라짐
    idx.close()


# ── 오케스트레이터 (정규화 + 시간 측정) ──────────────────────
def test_content_searcher_strips_filler_and_times(tmp_path):
    idx = _index(tmp_path)
    settings = Settings(content_default_limit=10, content_search_budget_ms=1500)
    searcher = ContentSearcher(idx, settings)

    resp = searcher.search(ContentSearchRequest(query="BRCA1 파일 찾아줘"))
    assert resp.terms == ["BRCA1"]  # '파일/찾아줘' 군더더기 제거
    assert resp.hits and resp.hits[0].name == "a.txt"
    assert not resp.over_budget
    assert resp.indexed_files == 2
    idx.close()
