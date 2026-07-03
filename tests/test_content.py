"""내용 검색 슬라이스 검증 — 라이브 SMB 없이 추출·FTS5·검색을 확인한다.

추출(extract)과 FTS5 저장/검색(content_index)은 순수 로직이라 SMB 없이 단위 테스트한다.
한국어 cp949 인코딩과 trigram 부분일치, 짧은 질의 LIKE 폴백을 함께 확인한다.
"""

from __future__ import annotations

import io
import zipfile

from smb_finder.config import Settings
from smb_finder import content_indexer
from smb_finder.content_index import ContentIndex
from smb_finder.content_search import ContentSearcher
from smb_finder.extract import extract_text
from smb_finder.models import ContentSearchRequest
from smb_finder.smb_client import FileEntry


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
    assert resp.result_count == len(resp.hits)
    assert not resp.over_budget
    assert resp.indexed_files == 2
    idx.close()


def test_content_search_budget_warning_does_not_log_raw_query(tmp_path, caplog):
    idx = _index(tmp_path)
    settings = Settings(content_default_limit=10, content_search_budget_ms=-1)
    searcher = ContentSearcher(idx, settings)
    sensitive_query = "SECRET_PATIENT_456 BRCA1 파일 찾아줘"

    with caplog.at_level("WARNING", logger="smb_finder.content_search"):
        resp = searcher.search(ContentSearchRequest(query=sensitive_query))

    assert resp.over_budget
    assert sensitive_query not in caplog.text
    assert "query_len=" in caplog.text
    idx.close()


def test_content_indexer_skips_unchanged_and_removes_stale(monkeypatch, tmp_path):
    class FakeSMBClient:
        read_count = 0

        def __init__(self, *args, **kwargs):
            self.files = [
                FileEntry(
                    path="A/keep.txt",
                    name="keep.txt",
                    ext=".txt",
                    size=4,
                    mtime=10.0,
                    abs_path="keep",
                ),
                FileEntry(
                    path="A/new.txt",
                    name="new.txt",
                    ext=".txt",
                    size=3,
                    mtime=20.0,
                    abs_path="new",
                ),
            ]

        def connect(self):
            return True

        def disconnect(self):
            return None

        def walk_files(self, **kwargs):
            return iter(self.files)

        def read_bytes(self, abs_path, max_bytes):
            type(self).read_count += 1
            return {"keep": b"keep", "new": b"new"}[abs_path]

    idx = ContentIndex(str(tmp_path / "content.fts.db"))
    idx.add(path="A/keep.txt", name="keep.txt", content="keep", ext=".txt", size=4, mtime=10.0)
    idx.add(path="A/stale.txt", name="stale.txt", content="stale", ext=".txt", size=5, mtime=9.0)
    idx.commit()
    idx.close()

    monkeypatch.setattr(content_indexer, "SMBClient", FakeSMBClient)
    settings = Settings(
        smb_host="configured-host",
        smb_share_name="configured-share",
        content_index_db_path=str(tmp_path / "content.fts.db"),
    )

    stats = content_indexer.build_content_index(settings, subpath="A")

    assert stats["indexed"] == 1
    assert stats["skipped_unchanged"] == 1
    assert stats["removed_stale"] == 1
    assert FakeSMBClient.read_count == 1

    idx = ContentIndex(str(tmp_path / "content.fts.db"))
    names = {h.name for h in idx.search(["new"], limit=10)}
    assert names == {"new.txt"}
    assert idx.search(["stale"], limit=10) == []
    idx.close()
