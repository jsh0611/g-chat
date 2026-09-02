#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
첨부파일 텍스트·표 추출기
====================================================================
- PDF  : pdfplumber 로 본문 텍스트 + 표(extract_tables)를 함께 추출
- HWP(v5) : pyhwp(hwp5html)로 HTML 변환 — LibreOffice HWP 필터는 이 형식을
  제대로 못 읽어 실패하므로 전용 라이브러리를 쓴다.
- HWPX/DOC(X)/XLS(X)/PPT(X)/ODT : LibreOffice(soffice)로 HTML 변환
그 뒤 공통적으로 기존 병합 셀(rowspan/colspan) 표 파서를 재사용해 표를 정확히 읽음.

표는 'A | B | C' 형태 행으로 평탄화해 '[표]' 블록으로 붙인다(RAG 친화적).

필요 도구
  · pip install pdfplumber        (PDF)
  · pip install pyhwp             (HWP v5) — 미설치 시 해당 첨부만 건너뜀
  · LibreOffice 설치(soffice)     (DOC/XLS/PPT/ODT/HWPX) — 미설치 시 해당 첨부만 건너뜀
"""

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from bs4 import BeautifulSoup

try:
    import pdfplumber  # type: ignore
    _HAS_PDF = True
except Exception:
    _HAS_PDF = False


# ── 공통: 표 격자 → 텍스트 ──────────────────────────────
def format_grid(grid: list) -> str:
    rows = []
    for row in grid:
        cells = [(c or "").strip() for c in row]
        if any(cells):
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def _bs_table_to_grid(table) -> list:
    """bs4 table → 병합 셀(rowspan/colspan) 펼친 직사각형 격자."""
    occupied, ncols, r = {}, 0, 0
    for tr in table.find_all("tr"):
        c = 0
        cells = tr.find_all(["td", "th"], recursive=False) or tr.find_all(["td", "th"])
        for cell in cells:
            while (r, c) in occupied:
                c += 1
            text = cell.get_text(" ", strip=True)
            rs = int(cell.get("rowspan", 1) or 1)
            cs = int(cell.get("colspan", 1) or 1)
            for dr in range(rs):
                for dc in range(cs):
                    occupied[(r + dr, c + dc)] = text
            c += cs
            ncols = max(ncols, c)
        r += 1
    grid = [[occupied.get((i, j), "") for j in range(ncols)] for i in range(r)]
    return [row for row in grid if any(x.strip() for x in row)]


def _norm(t: str) -> str:
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


# ── PDF ─────────────────────────────────────────────────
_PDF_TIMEOUT = 120      # PDF 1개 추출 제한(초). pdfplumber 의 텍스트기반 표추출은
                        # 텍스트가 빽빽한 PDF 에서 사실상 무한정 멈추기도 한다 → 하드 타임아웃.
_PDF_MAX_PAGES = 1000   # 이 이상 페이지는 처리하지 않음(초대형 폭주만 방지)
_MAX_ATTACH_CHARS = 3_000_000   # 첨부 1개당 추출 텍스트 상한(글자). 정상 텍스트 문서는
                        # 여기에 안 걸린다(수백 페이지도 OK). 진짜 병적인 폭주만 방지하는 안전장치.


def _pdf_page_texts(path: Path):
    """페이지별로 추출 텍스트 조각을 순차 yield. (증분 저장용 — 중간에 끊겨도 앞부분 보존)"""
    with pdfplumber.open(path) as pdf:
        npages = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            if i >= _PDF_MAX_PAGES:
                yield f"[...{npages}페이지 중 {_PDF_MAX_PAGES}페이지까지만 추출...]"
                return
            chunk = []
            txt = page.extract_text() or ""
            if txt.strip():
                chunk.append(txt.strip())
            tables = page.extract_tables()
            # 테두리 없는 표는 텍스트정렬 기반으로 재시도하되, 텍스트가 매우 많은 페이지는
            # 이 방식이 폭주(사실상 무한정)하므로 건너뛴다 — PDF 멈춤의 주원인.
            if not tables and len(txt) < 3000:
                try:
                    tables = page.extract_tables(
                        {"vertical_strategy": "text", "horizontal_strategy": "text"})
                except Exception:
                    tables = []
            for t in (tables or []):
                g = format_grid(t)
                if g:
                    chunk.append("[표]\n" + g)
            if chunk:
                yield "\n\n".join(chunk)


def _extract_pdf_inproc(path: Path) -> str:
    """같은 프로세스에서 전체 추출(테스트/워커 외 직접 호출용)."""
    parts, n = [], 0
    for part in _pdf_page_texts(path):
        parts.append(part); n += len(part)
        if n >= _MAX_ATTACH_CHARS:
            parts.append("[...분량 초과로 이후 생략...]"); break
    return _norm("\n\n".join(parts))


def extract_pdf(path: Path) -> str:
    if not _HAS_PDF:
        print(f"    · pdfplumber 미설치 → PDF 건너뜀: {path.name}")
        return ""
    # 타임아웃을 걸 수 있도록 별도 프로세스로 격리 실행(파이썬 내 C 호출은 스레드 타임아웃 불가).
    # 워커가 페이지마다 결과를 flush 하므로, 타임아웃으로 강제 종료돼도 앞부분은 보존된다.
    out = Path(tempfile.mkdtemp(prefix="pdf_")) / "out.txt"
    timed_out = False
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "preprocessor.extractors", "--pdf-worker",
             str(path), str(out)],
            capture_output=True, timeout=_PDF_TIMEOUT)
        rc, err = proc.returncode, (proc.stderr or b"").decode("utf-8", "replace").strip()
    except subprocess.TimeoutExpired:
        timed_out, rc, err = True, None, ""
    except Exception as e:
        print(f"    ! PDF 추출 실행 실패 {path.name}: {e}")
        shutil.rmtree(out.parent, ignore_errors=True)
        return ""
    try:
        text = out.read_text(encoding="utf-8", errors="replace") if out.exists() else ""
        if timed_out:
            # 전량 유실 대신 '그때까지 추출한 부분'을 보존한다.
            print(f"    ! PDF 시간 초과({_PDF_TIMEOUT}s) → {len(text)}자까지만 추출: {path.name}")
        elif rc != 0:
            print(f"    ! PDF 추출 실패 {path.name}: {err[-200:]}")
            return ""
        return _norm(text)
    finally:
        shutil.rmtree(out.parent, ignore_errors=True)


# ── HWP v5 전용 경로 (pyhwp / hwp5html) ─────────────────
# 국내 관공서 .hwp 는 대부분 OLE 기반 HWP v5 형식인데, LibreOffice 의 HWP 필터는
# 이 형식을 제대로 못 읽어 'source file could not be loaded' 로 통째로 실패한다.
# → HWP v5 전용 라이브러리 pyhwp 의 hwp5html 로 변환한다(표 포함 HTML 생성).
#   설치: pip install pyhwp
_HWP_TIMEOUT = 120


def _hwp5html_exe() -> str | None:
    """hwp5html 콘솔 스크립트 경로. 현재 파이썬의 Scripts/bin 우선 탐색.
    (python -m hwp5.hwp5html 형태는 진입점이 달라 --output 이 동작하지 않으므로 콘솔 스크립트를 쓴다.)"""
    exe = shutil.which("hwp5html")
    if exe:
        return exe
    base = Path(sys.executable).parent
    for cand in (base / "Scripts" / "hwp5html.exe", base / "hwp5html.exe",
                 base / "Scripts" / "hwp5html", base / "bin" / "hwp5html"):
        if cand.exists():
            return str(cand)
    return None


def extract_hwp(path: Path) -> str:
    # pyhwp(hwp5)는 OLE 기반 HWP v5 전용이다. 구형 'HWP Document File V3.00'
    # 등 비-OLE 파일은 "Not an OLE2 Compound Binary File" 로 실패하는데, 이 옛 형식은
    # 오히려 LibreOffice HWP 필터가 읽어낸다. → pyhwp 실패 시 LibreOffice 로 폴백.
    exe = _hwp5html_exe()
    if exe:
        workdir = Path(tempfile.mkdtemp(prefix="hwp5_"))
        try:
            outf = workdir / "doc.html"
            try:
                proc = subprocess.run([exe, "--html", "--output", str(outf), str(path)],
                                      capture_output=True, timeout=_HWP_TIMEOUT)
            except subprocess.TimeoutExpired:
                print(f"    ! HWP 변환 시간 초과({_HWP_TIMEOUT}s) → 건너뜀: {path.name}")
                return ""
            except Exception as e:
                print(f"    ! HWP 변환 실행 실패 {path.name}: {e}")
                proc = None
            if proc is not None and proc.returncode == 0 and outf.exists():
                soup = BeautifulSoup(outf.read_text(encoding="utf-8", errors="replace"),
                                     "html.parser")
                return _office_html_to_text(soup)
            # pyhwp 실패 → 구형 HWP(v3 등)일 수 있으니 LibreOffice 로 재시도
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    text = extract_via_office(path)   # LibreOffice 폴백 (구형 HWP·미설치 대비)
    if text:
        return text
    if not exe:
        print(f"    · HWP 추출 실패(pyhwp 미설치 + LibreOffice 실패) → 건너뜀: {path.name}")
    else:
        print(f"    ! HWP 추출 실패(pyhwp·LibreOffice 모두 실패) → 건너뜀: {path.name}")
    return ""


# ── LibreOffice 변환 경로 (DOC/XLS/PPT/ODT 등) ──────────
# LibreOffice 실행파일 후보 경로 (PATH 미등록 대비 — Windows/맥/리눅스 기본 설치 위치)
_SOFFICE_CANDIDATES = [
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",   # macOS
    "/usr/bin/soffice", "/usr/local/bin/soffice",              # Linux
]

_LO_TIMEOUT = 120  # 파일당 변환 제한(초). 손상된 파일 하나가 전체 파이프라인을 물고 늘어지는 것 방지


def _soffice() -> str | None:
    # 1) PATH 에 있으면 그대로 사용
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if exe:
        return exe
    # 2) 없으면 기본 설치 경로에서 탐색 (Windows 등 PATH 미등록 대비)
    for cand in _SOFFICE_CANDIDATES:
        if Path(cand).exists():
            return cand
    return None


def _convert_to_html(exe: str, path: Path, workdir: Path) -> Path | None:
    """LibreOffice headless 변환.

    파일마다 새 프로세스를 띄우면서 기본(공유) 사용자 프로필을 그대로 쓰면,
    이전 호출이 남긴 lock 파일 때문에 '이미 실행 중' 오류가 나거나 응답 없이
    멈춰있다 타임아웃되는 경우가 매우 흔하다(윈도우에서 특히 잦음).
    → 호출마다 임시 프로필(UserInstallation)을 새로 만들어 완전히 격리한다.
    """
    outdir = workdir / "out"
    profile_dir = workdir / "profile"
    outdir.mkdir()
    profile_dir.mkdir()

    cmd = [
        exe, "--headless", "--invisible", "--norestore", "--nologo",
        "--nofirststartwizard", "--nolockcheck",
        f"-env:UserInstallation={profile_dir.as_uri()}",
        "--convert-to", "html",   # 필터명을 고정하지 않아야 doc/hwp/xls/ppt 등 문서 종류별로
        "--outdir", str(outdir), str(path),   # 맞는 기본 필터가 자동 선택됨(수동 지정 시 타입 불일치로 실패 가능)
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=_LO_TIMEOUT)
    except subprocess.TimeoutExpired:
        print(f"    ! LibreOffice 변환 시간 초과({_LO_TIMEOUT}s) → 건너뜀: {path.name}")
        return None
    except Exception as e:
        print(f"    ! LibreOffice 실행 실패 {path.name}: {e}")
        return None

    htmls = list(outdir.glob("*.html")) + list(outdir.glob("*.htm"))
    if proc.returncode != 0 or not htmls:
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        print(f"    ! LibreOffice 변환 실패 {path.name} (code={proc.returncode}) {err[:300]}")
        return None
    return htmls[0]


def extract_via_office(path: Path) -> str:
    exe = _soffice()
    if not exe:
        print(f"    · LibreOffice(soffice) 미설치 → 건너뜀: {path.name} "
              f"(HWP 표 추출하려면 LibreOffice 설치 필요)")
        return ""

    workdir = Path(tempfile.mkdtemp(prefix="lo_"))
    try:
        html = _convert_to_html(exe, path, workdir)
        if not html:
            return ""
        soup = BeautifulSoup(html.read_text(encoding="utf-8", errors="replace"), "html.parser")
        return _office_html_to_text(soup)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)   # 임시 out/profile 디렉터리 정리(디스크 누수 방지)


def _office_html_to_text(soup: BeautifulSoup) -> str:
    body = soup.body or soup

    # 1) 말단(leaf) 표만 격자로 추출. 중첩 표를 포함한 '레이아웃 표'는 데이터 표가 아니다.
    #    (특히 HWP→HTML 은 문서 전체를 레이아웃 표로 감싸고 그 안에 데이터 표를 중첩한다.)
    leaf_tables = [t for t in body.find_all("table") if not t.find("table")]
    tables = []
    for table in leaf_tables:
        g = format_grid(_bs_table_to_grid(table))
        if g:
            tables.append(g)
    # 2) 방금 격자로 뽑은 '말단 표'만 제거(본문과 중복 방지). 레이아웃(외곽) 표는
    #    본문 텍스트를 담고 있으므로 남긴다 — 모든 표를 지우면 표로 감싸진 본문까지 사라진다.
    for table in leaf_tables:
        table.decompose()
    prose = body.get_text("\n", strip=True)

    parts = []
    if prose.strip():
        parts.append(prose.strip())
    parts += ["[표]\n" + g for g in tables]
    return _norm("\n\n".join(parts))


# ── dispatch ────────────────────────────────────────────
# .hwp(HWP v5) 는 pyhwp 전용 경로로, 나머지 오피스 포맷은 LibreOffice 로 처리.
_OFFICE = {".doc", ".docx", ".xls", ".xlsx",
           ".ppt", ".pptx", ".odt", ".ods", ".odp"}


def extract(path) -> str:
    path = Path(path)
    if not path.exists():
        return ""
    suf = path.suffix.lower()
    if suf == ".pdf":
        text = extract_pdf(path)
    elif suf == ".hwp":
        text = extract_hwp(path)
    elif suf == ".hwpx":
        # HWPX(XML zip) 는 pyhwp(hwp5) 미지원 → LibreOffice 로 시도(best-effort)
        text = extract_via_office(path)
    elif suf in _OFFICE:
        text = extract_via_office(path)
    elif suf in (".txt", ".csv"):
        raw = path.read_bytes()
        if b"\x00" in raw[:4096]:      # 널바이트 = 사실상 바이너리 → 텍스트 추출 안 함
            return ""
        text = _norm(raw.decode("utf-8", errors="replace"))
    else:
        return ""
    # 방어적 상한: 어떤 경로든 첨부 1개가 과도하게 커지면 잘라 비대화를 막는다.
    if len(text) > _MAX_ATTACH_CHARS:
        text = text[:_MAX_ATTACH_CHARS] + "\n[...분량 초과로 생략...]"
    return text


# ── 서브프로세스 워커 (PDF 타임아웃 격리용) ─────────────
if __name__ == "__main__":
    # 사용법: python -m preprocessor.extractors --pdf-worker <입력.pdf> <출력.txt>
    # 페이지마다 결과를 즉시 기록·flush → 부모가 타임아웃으로 종료해도 앞부분은 파일에 남는다.
    if len(sys.argv) == 4 and sys.argv[1] == "--pdf-worker":
        _n = 0
        with open(sys.argv[3], "w", encoding="utf-8") as _f:
            if _HAS_PDF:
                for _part in _pdf_page_texts(Path(sys.argv[2])):
                    _f.write(_part + "\n\n")
                    _f.flush()
                    _n += len(_part)
                    if _n >= _MAX_ATTACH_CHARS:
                        _f.write("[...분량 초과로 이후 생략...]\n"); break