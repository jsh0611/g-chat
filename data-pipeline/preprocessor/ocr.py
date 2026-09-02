#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
독립 OCR 스크립트 (Tesseract) — 이미지·스캔 PDF 에서 글자 뽑아내기
====================================================================
이 스크립트는 '이미 다운로드된' 이미지/스캔 PDF 파일에서 글자를 인식(OCR)해
텍스트로 만든다. 기존 파이프라인(crawl→parse→preprocess)과 **완전히 분리**돼 있어,
이걸 돌려도 이미 잘 나온 data/processed/documents.jsonl 은 건드리지 않는다.

OCR 엔진으로 Tesseract 를 쓴다(Google 후원 오픈소스). 언어 데이터를 로컬에 직접 두므로
외부 서버에서 모델을 받아오지 않는다(공급망·프라이버시 부담 적음). 모든 처리는 로컬.

── 왜 이렇게 설계했나 (지난 작업의 교훈) ──────────────────────────
1. 네트워크 안 씀    : 파일은 이미 data/raw/files/ 에 있다. 디스크만 읽는다(재크롤링 X).
2. 별도 산출물       : 결과는 data/processed/ocr_results.jsonl 로 따로 쓴다.
3. 이어하기(캐싱)    : 이미 OCR 한 파일은 건너뛴다. 중간에 멈춰도 처음부터 다시 안 한다.
4. 파일별 격리       : 한 파일에서 오류가 나도 try/except 로 감싸 전체가 죽지 않는다.
5. 페이지 상한       : 스캔 PDF 는 페이지 수를 제한해 폭주를 막는다.
6. UTF-8 출력        : 윈도우 인코딩(cp949) 문제로 죽지 않게 출력을 UTF-8 로 고정.

── 대상 ────────────────────────────────────────────────────────────
- 이미지 파일(.jpg/.jpeg/.png/.gif/.bmp/.tif) : 무조건 OCR
- PDF 중 '텍스트 레이어가 없는 스캔본'          : 페이지를 이미지로 렌더링 후 OCR
  (텍스트가 이미 들어있는 일반 PDF 는 본 파이프라인이 처리했으므로 건너뜀)

── 설치 ────────────────────────────────────────────────────────────
1) Tesseract 본체(시스템 프로그램) 설치 — pip 아님:
   Windows: https://github.com/UB-Mannheim/tesseract/wiki 의 인스톨러로 설치
            (설치 중 'Additional language data'에서 Korean 체크 → kor.traineddata 포함)
   Ubuntu : sudo apt install tesseract-ocr tesseract-ocr-kor
2) 파이썬 래퍼(가상환경 agent_env 안에서):
   pip install pytesseract pillow
   (pypdfium2 는 이미 설치돼 있음 — 스캔 PDF 렌더링용)

── 실행 (agent_env 활성화 + 프로젝트 폴더 안에서) ──────────────────
  python -m preprocessor.ocr            # 전체(이미지 + 스캔 PDF)
  python -m preprocessor.ocr --images   # 이미지만
  python -m preprocessor.ocr --pdf      # 스캔 PDF만
  python -m preprocessor.ocr --limit 20 # 테스트: 앞 20개만
"""

import json
import re
import shutil
import sys
import time
import traceback
from pathlib import Path

import config as C

# 윈도우 콘솔/리다이렉트 시 한글·기호 출력이 깨지거나 죽지 않게 UTF-8 로 고정.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# ── 설정값 ──────────────────────────────────────────────
_IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff"}
_OCR_LANG = "kor+eng"         # 한국어 + 영어(공고문에 영문·숫자 섞여 있으므로 함께)
_TESS_CONFIG = "--oem 1 --psm 3"   # oem1=LSTM 엔진, psm3=자동 페이지 분할(일반 문서에 무난)
_PDF_RENDER_SCALE = 4.0       # 스캔 PDF→이미지 확대 배율. Tesseract 는 고해상도(≈300DPI)에서 정확도↑
_PDF_MAX_PAGES = 30           # 스캔 PDF 는 최대 이 페이지까지만 OCR(폭주 방지)
_PDF_TEXTLAYER_MIN = 30       # 앞부분에서 이만큼 미만의 글자만 나오면 '스캔본'으로 간주
_OUT_PATH = C.PROCESSED_DIR / "ocr_results.jsonl"

# Tesseract 실행파일 후보 경로(PATH 미등록 대비)
_TESS_CANDIDATES = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    "/usr/bin/tesseract", "/usr/local/bin/tesseract",
]


# ── Tesseract 준비 ──────────────────────────────────────
_ready = False


def _ensure_tesseract():
    """pytesseract 가 tesseract 실행파일을 찾도록 경로를 설정(한 번만)."""
    global _ready
    if _ready:
        return
    import pytesseract
    exe = shutil.which("tesseract")
    if not exe:
        for cand in _TESS_CANDIDATES:
            if Path(cand).exists():
                exe = cand
                break
    if exe:
        pytesseract.pytesseract.tesseract_cmd = exe
    else:
        print("! Tesseract 실행파일을 못 찾음 — 설치 후 PATH 등록하거나 _TESS_CANDIDATES 에 경로 추가")
    _ready = True


def _image_to_text(pil_image) -> str:
    """PIL 이미지 → Tesseract 텍스트. 흑백 변환으로 인식률을 조금 높인다."""
    import pytesseract
    _ensure_tesseract()
    gray = pil_image.convert("L")   # 흑백(그레이스케일)로 바꾸면 배경 노이즈 영향이 줄어 인식↑
    return pytesseract.image_to_string(gray, lang=_OCR_LANG, config=_TESS_CONFIG).strip()


def ocr_image_file(path: Path) -> str:
    """이미지 파일 하나 → 인식된 텍스트."""
    from PIL import Image
    with Image.open(path) as im:
        return _image_to_text(im)


def _pdf_has_text_layer(path: Path) -> bool:
    """PDF 에 '진짜 텍스트'가 들어있는지 빠르게 확인.
    앞쪽 몇 페이지에서 글자가 충분히 나오면 텍스트 PDF(=OCR 불필요)로 본다."""
    try:
        import pdfplumber
    except ImportError:
        return False
    try:
        with pdfplumber.open(path) as pdf:
            n = 0
            for page in pdf.pages[:3]:
                n += len((page.extract_text() or "").strip())
                if n >= _PDF_TEXTLAYER_MIN:
                    return True
    except Exception:
        return False
    return False


def ocr_pdf_file(path: Path) -> str:
    """스캔 PDF → 각 페이지를 이미지로 렌더링해 OCR → 합침."""
    import pypdfium2 as pdfium   # 페이지→이미지 렌더링(이미 설치돼 있음)

    parts = []
    pdf = pdfium.PdfDocument(str(path))
    try:
        npages = min(len(pdf), _PDF_MAX_PAGES)
        for i in range(npages):
            page = pdf[i]
            bitmap = page.render(scale=_PDF_RENDER_SCALE)   # 고해상도로 렌더링
            pil = bitmap.to_pil()
            txt = _image_to_text(pil)
            if txt:
                parts.append(f"[p.{i+1}]\n{txt}")
        if len(pdf) > _PDF_MAX_PAGES:
            parts.append(f"[...총 {len(pdf)}쪽 중 {_PDF_MAX_PAGES}쪽까지만 OCR...]")
    finally:
        pdf.close()
    return "\n\n".join(parts).strip()


# ── 품질 판정 ───────────────────────────────────────────
def _assess_quality(text: str):
    """OCR 결과가 '쓸 만한지'를 대략 판정한다.
    디자인 포스터 등은 꾸민 글씨체 때문에 한두 글자짜리 파편이 잔뜩 나오는데,
    이런 쓰레기를 걸러내려고 두 지표를 쓴다:
      · hangul_ratio : 글자 중 한글 음절의 비율(파편/기호가 많으면 낮음)
      · word_count   : 2글자 이상 한글 '단어' 개수(의미있는 텍스트인지)
    반환: ("ok"|"low"|"empty", 한글비율, 단어수)"""
    if not text.strip():
        return "empty", 0.0, 0
    hangul = len(re.findall(r"[가-힣]", text))
    alnum = len(re.findall(r"[가-힣A-Za-z0-9]", text)) or 1
    ratio = hangul / alnum
    words = len(re.findall(r"[가-힣]{2,}", text))
    quality = "ok" if (ratio >= 0.4 and words >= 3) else "low"
    return quality, round(ratio, 2), words


# ── 대상 수집 ───────────────────────────────────────────
def _iter_attachment_targets():
    """manifest.json 을 읽어 (파일경로, 참조문서정보) 를 중복 없이 돌려준다.
    같은 파일이 여러 글에 첨부돼 있으면 한 번만 OCR 하고, 참조한 글들을 함께 기록한다."""
    man = json.loads((C.RAW_DIR / "manifest.json").read_text(encoding="utf-8"))
    seen = {}   # file(rel) -> {"path":.., "refs":[..]}
    for it in man:
        for a in it.get("attachments", []):
            rel = a.get("file")
            if not rel:
                continue
            ref = {"doc_id": it.get("html_key") or it.get("idx"),
                   "title": it.get("title") or a.get("name") or "",
                   "url": it.get("url", ""),
                   "source": it.get("board") or it.get("facility") or it.get("type")}
            if rel not in seen:
                seen[rel] = {"path": C.DATA / rel,
                             "name": a.get("name") or Path(rel).name, "refs": [ref]}
            else:
                seen[rel]["refs"].append(ref)
    return seen


def _load_done() -> set:
    """이미 처리한 파일 목록(캐시). 이어하기용."""
    done = set()
    if _OUT_PATH.exists():
        for ln in _OUT_PATH.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(ln)["file"])
            except Exception:
                pass
    return done


# ── 메인 ────────────────────────────────────────────────
def run_ocr(do_images=True, do_pdf=True, limit=None):
    C.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    targets = _iter_attachment_targets()
    done = _load_done()

    todo = []
    for rel, info in targets.items():
        if rel in done:
            continue
        ext = info["path"].suffix.lower()
        if ext in _IMG_EXT and do_images:
            todo.append(("image", rel, info))
        elif ext == ".pdf" and do_pdf:
            todo.append(("pdf", rel, info))
    if limit:
        todo = todo[:limit]

    print(f"[OCR] 대상 {len(todo)}개 (이미 완료 {len(done)}개는 건너뜀) → {_OUT_PATH.name}")
    if not todo:
        print("[OCR] 처리할 새 파일이 없습니다.")
        return 0

    # 결과는 append 로 한 건씩 즉시 기록·flush → 중간에 멈춰도 그때까지는 저장됨
    n_ok = n_skip = n_err = n_lowq = 0
    with open(_OUT_PATH, "a", encoding="utf-8") as out:
        for k, (kind, rel, info) in enumerate(todo, 1):
            path = info["path"]
            t0 = time.time()
            try:
                if not path.exists():
                    n_skip += 1
                    continue
                if kind == "pdf":
                    # 텍스트 레이어가 있으면 스캔본이 아니므로 건너뜀(본 파이프라인이 이미 처리)
                    if _pdf_has_text_layer(path):
                        n_skip += 1
                        continue
                    text = ocr_pdf_file(path)
                else:
                    text = ocr_image_file(path)
            except Exception as e:
                n_err += 1
                print(f"  ! [{k}/{len(todo)}] 실패 {path.name}: {e}")
                traceback.print_exc()
                continue

            # 품질 판정: 쓸 만하면 'ok', 파편투성이면 'low' 로 태깅(데이터는 버리지 않음)
            quality, ratio, words = _assess_quality(text)
            if quality != "ok":
                n_lowq += 1
            rec = {"file": rel, "name": info["name"], "kind": kind,
                   "quality": quality, "hangul_ratio": ratio, "word_count": words,
                   "ocr_text": text, "refs": info["refs"]}
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()   # 즉시 디스크에 기록(이어하기 안전)
            n_ok += 1
            if k % 10 == 0 or k == len(todo):
                print(f"  [{k}/{len(todo)}] {path.name[:28]} → {len(text)}자 [{quality}] ({time.time()-t0:.1f}s)")

    print(f"[OCR] 완료 — 처리 {n_ok} (그중 저품질 {n_lowq}), 건너뜀 {n_skip}, 실패 {n_err}")
    print(f"       → {_OUT_PATH}")
    print(f"       (병합 시 quality=='ok' 만 쓰면 쓰레기 자동 제외됨)")
    return n_ok


if __name__ == "__main__":
    args = sys.argv[1:]
    only_img = "--images" in args
    only_pdf = "--pdf" in args
    lim = None
    if "--limit" in args:
        try:
            lim = int(args[args.index("--limit") + 1])
        except (IndexError, ValueError):
            lim = None
    run_ocr(do_images=not only_pdf, do_pdf=not only_img, limit=lim)
