#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
원본 HTML 저장소 (SQLite)
====================================================================
페이지당 .html 파일을 흩어놓지 않고 data/raw/raw.db 한 파일에 저장한다.
  · 파일 개수(inode) 폭증 방지, 이동·백업 용이 (현업 raw layer 방식)
  · key(예: board_notice001_11117) 로 저장/조회
첨부파일은 pdfplumber/LibreOffice 가 디스크에서 읽어야 하므로 파일로 둔다.
"""

import sqlite3

import config as C

_conn = None


def _c():
    global _conn
    if _conn is None:
        C.RAW_DIR.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(C.RAW_DIR / "raw.db"))
        _conn.execute("CREATE TABLE IF NOT EXISTS pages("
                      "key TEXT PRIMARY KEY, html TEXT, fetched_at TEXT)")
        _conn.commit()
    return _conn


def save_html(key: str, html: str) -> str:
    c = _c()
    c.execute("INSERT OR REPLACE INTO pages(key, html, fetched_at) "
              "VALUES(?, ?, datetime('now'))", (key, html))
    c.commit()
    return key


def load_html(key: str) -> str | None:
    row = _c().execute("SELECT html FROM pages WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def has_html(key: str) -> bool:
    if not key:
        return False
    return _c().execute("SELECT 1 FROM pages WHERE key=?", (key,)).fetchone() is not None


def count() -> int:
    return _c().execute("SELECT COUNT(*) FROM pages").fetchone()[0]