#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
파이프라인 실행기
====================================================================
  python main.py                # 전체: 수집 → 구조화 → 전처리
  python main.py crawl          # 수집만 (증분: 새 글만 추가)
  python main.py parse          # 구조화만 (수집 결과 필요)
  python main.py preprocess     # 전처리만 (구조화 결과 필요)
  python main.py crawl --no-download   # HTML 만 받고 첨부 다운로드 생략
  python main.py crawl --reset         # 기존 manifest 무시하고 처음부터
"""

import sys

# Windows 콘솔/파일 리다이렉트 시 기본 인코딩(cp949)으로는 '↳ ✔ ⚠' 등을 못 찍어
# UnicodeEncodeError 로 파이프라인 전체가 죽는다(수집은 끝났는데 parse/preprocess 미실행).
# 출력 스트림을 UTF-8 로 고정해 방지.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from crawler.crawler import run_crawl
from parser.parser import run_parse
from preprocessor.preprocessor import run_preprocess


def main(argv):
    stage = argv[1] if len(argv) > 1 and not argv[1].startswith("-") else "all"
    download = "--no-download" not in argv
    full_reset = "--reset" in argv

    if stage in ("all", "crawl"):
        run_crawl(download=download, full_reset=full_reset)
    if stage in ("all", "parse"):
        run_parse()
    if stage in ("all", "preprocess"):
        run_preprocess()

    if stage not in ("all", "crawl", "parse", "preprocess"):
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv)