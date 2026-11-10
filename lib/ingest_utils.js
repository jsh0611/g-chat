// ingest.js(공단 홈페이지)와 ingest_camping.js(관광지 통합예약시스템)가 함께 쓰는
// 표 정규화·날짜 정규화·청킹 함수. 두 출처가 같은 규칙으로 청킹·임베딩되도록 한 곳에 둔다.
import crypto from 'crypto';
import fs from 'fs';
import { restructureTables } from './table_utils.js';
import { EmbedQuotaError, budgetLeft, usedToday, pacificDate } from './embed_utils.js';

/**
 * 새 크롤러 표 형식 → 레코드 문장 변환
 *  - "[표]" 마커 줄 제거
 *  - " | " 구분 줄 블록을 마크다운 표(|a|b|)로 감싼 뒤 restructureTables() 적용
 *  - 셀 전체가 같은 값 반복인 줄(colspan 병합 풀림, 예: "14시 수업 | 14시 수업 | ...")은 셀 하나로 축약
 */
export function normalizeTables(content) {
  const lines = content.split('\n').filter((l) => l.trim() !== '[표]');
  const wrapped = lines.map((line) => {
    if (!line.includes(' | ')) return line;
    let cells = line.split('|').map((c) => c.trim());
    // colspan 풀림 축약: 모든 셀이 동일하면 하나만 남긴다
    const nonEmpty = cells.filter(Boolean);
    if (nonEmpty.length > 1 && nonEmpty.every((c) => c === nonEmpty[0])) {
      cells = [nonEmpty[0]];
      return cells[0]; // 셀 1개는 표로 감싸지 않고 본문 줄로
    }
    return `| ${cells.join(' | ')} |`;
  });
  return restructureTables(wrapped.join('\n'));
}

/** 다양한 표기의 날짜를 "YYYY-MM-DD"로 정규화. 해석 불가하면 null */
export function normalizeDate(raw) {
  if (!raw) return null;
  const m = raw.match(/(\d{4})\s*[.\-\/년]\s*(\d{1,2})\s*[.\-\/월]\s*(\d{1,2})/);
  if (!m) return null;
  const [, y, mo, d] = m;
  return `${y}-${mo.padStart(2, '0')}-${d.padStart(2, '0')}`;
}

/**
 * 텍스트를 줄 단위로 청크 분할
 * 문자 수로 자르되 줄 중간에서 자르지 않는다 — 표 레코드 한 줄이 반토막 나는 것을 방지.
 * 단일 줄이 chunkSize보다 길면 그 줄만 문자 단위로 분할.
 */
export function chunkText(text, chunkSize = 500) {
  const lines = text.split('\n');
  const chunks = [];
  let buf = '';

  const flush = () => {
    if (buf.trim()) chunks.push(buf.trim());
    buf = '';
  };

  for (const line of lines) {
    if (line.length > chunkSize) {
      flush();
      for (let i = 0; i < line.length; i += chunkSize) {
        chunks.push(line.slice(i, i + chunkSize));
      }
      continue;
    }
    if (buf.length + line.length + 1 > chunkSize) flush();
    buf += (buf ? '\n' : '') + line;
  }
  flush();
  return chunks;
}

/** 청크 임베딩 입력 = 제목(+게시일) 헤더 + 본문 — 제목 기반 검색 정확도·옛 공지 구분용 */
export function withHeader(doc, body) {
  const header = [
    doc.title ? `제목: ${doc.title}` : null,
    doc.date ? `게시일: ${doc.date}` : null,
  ].filter(Boolean).join('\n');
  return header ? `${header}\n\n${body}` : body;
}

/**
 * 중복 제거 '적용' 결과(dedup_exclusions.json)에서 이 출처의 제외 doc_id 집합을 읽는다.
 * 파일이 없으면 빈 집합 → 아무것도 빠지지 않는다. 원본 jsonl 은 그대로 두고 적재 단계에서만
 * 건너뛰므로, 되돌리려면 이 파일에서 항목을 지우고(또는 파일 삭제) 재적재하면 된다.
 * 생성: python data-pipeline/dedup.py --apply  (dry-run 확인 후)
 */
export function loadExclusions(source, file = 'dedup_exclusions.json') {
  if (!fs.existsSync(file)) return new Set();
  const { excluded = [] } = JSON.parse(fs.readFileSync(file, 'utf-8'));
  return new Set(excluded.filter((e) => e.source === source).map((e) => e.doc_id));
}

// ─────────────────────────────────────────────
// 긴 문서 처리 — 길이 때문에 문서 전체가 빠지는 일은 없게 한다 (2026-10 원칙)
// ─────────────────────────────────────────────
// · 대장류(공문 제목 목록·등록대장·정보공개 목록)는 챗봇 답변에 못 쓰는 덤프라 제목으로 거른다.
//   (예전엔 10만 자 초과 문서를 통째로 버려, 갱신된 '고객만족도'(OCR 포함)까지 빠질 뻔했다)
// · 그 밖의 문서는 첨부 하나당 앞부분 ATTACH_CAP 자만 넣는다. OCR 이미지 블록([이미지: …])은 자르지 않는다.
// ※ data-pipeline/dedup.py 의 load() 가 같은 규칙을 쓴다 — 바꾸면 함께 바꿀 것.
export const LEDGER_TITLE = /정보목록|기록물\s*등록\s*대장|비공개\s*대상\s*정보\s*세부기준|정보공개\s*(공개\s*및\s*비공개\s*목록|청구\s*및\s*처리\s*현황)/;
export const ATTACH_CAP = 30000;
export const isLedger = (title) => LEDGER_TITLE.test(title ?? '');

// 버전이 여러 개 첨부된 문서: 현행만 넣는다 — 옛 연도 버전이 섞이면 '지금 기준' 질문에 옛 조항으로 답한다.
//   본문(현행) + 각 패턴의 최신 연도 첨부 1개만 남기고, 나머지 첨부(옛 연도·서식)는 뺀다.
//   ※ 문서별 예외(일반 규칙 아님). data-pipeline/dedup.py 의 VERSIONED_DOCS 와 함께 바꿀 것.
export const VERSIONED_DOCS = [
  { title: /^개인정보처리방침$/, keep: [/(\d{4})년\s*개인정보\s*처리방침/, /(\d{4})년\s*방침개정/] },
];

/** 반환 { content, dropped:[첨부 이름] } — 대상 문서가 아니면 그대로 */
export function keepLatestVersions(title, content) {
  const rule = VERSIONED_DOCS.find((r) => r.title.test(title ?? ''));
  if (!rule) return { content, dropped: [] };
  const marks = [...content.matchAll(/\n\n\[(첨부|이미지): ([^\]\n]*)\]/g)];
  if (!marks.length) return { content, dropped: [] };
  const latest = rule.keep.map((re) => {
    const years = marks.map((m) => Number(m[2].match(re)?.[1])).filter(Boolean);
    return years.length ? Math.max(...years) : null;
  });
  const keepName = (name) => rule.keep.some((re, i) => Number(name.match(re)?.[1]) === latest[i]);
  const dropped = [];
  let out = content.slice(0, marks[0].index);
  marks.forEach((m, i) => {
    const end = i + 1 < marks.length ? marks[i + 1].index : content.length;
    if (m[1] === '첨부' && !keepName(m[2])) dropped.push(m[2]);
    else out += content.slice(m.index, end);
  });
  return { content: out, dropped };
}

/** 첨부 블록("\n\n[첨부: 이름]\n…")마다 본문을 앞 cap 자로 자른다. 반환 { content, cuts:[{name, from, to}] } */
export function capAttachments(content, cap = ATTACH_CAP) {
  const re = /\n\n\[(첨부|이미지): ([^\]\n]*)\]/g;
  const marks = [...content.matchAll(re)];
  if (!marks.some((m) => m[1] === '첨부')) return { content, cuts: [] };
  const cuts = [];
  let out = content.slice(0, marks[0].index);
  marks.forEach((m, i) => {
    const end = i + 1 < marks.length ? marks[i + 1].index : content.length;
    let block = content.slice(m.index, end);
    const head = `\n\n[${m[1]}: ${m[2]}]`;
    const body = block.slice(head.length);
    if (m[1] === '첨부' && body.length > cap) {
      cuts.push({ name: m[2], from: body.length, to: cap });
      block = `${head}${body.slice(0, cap)}\n[...첨부 앞부분 ${cap.toLocaleString()}자만 수록 — 전체 ${body.length.toLocaleString()}자...]`;
    }
    out += block;
  });
  return { content: out, cuts };
}

/** 옛 문서 ID → 고정 키 (2026-10 키 이전: 페이지 ID 를 목록 위치 뺀 URL 기준으로 바꿈).
 *  2026-10 고정 키 이관 때 1회 생성한 doc_key_aliases.json. 없으면 빈 맵. */
export function loadAliases(source, file = 'doc_key_aliases.json') {
  if (!fs.existsSync(file)) return new Map();
  const all = JSON.parse(fs.readFileSync(file, 'utf-8'));
  return new Map(Object.entries(all[source] ?? {}));
}

// 두 번 연속 수집에서 안 보인 문서만 지운다 — 한 번 안 보인 건 수집 누락일 수 있어 기존 행 유지.
//   (수집엔 있는데 필터·제외 목록으로 빠진 문서는 의도된 제외라 바로 지운다)
const STATE_FILE = 'ingest_state.json';
const MISS_LIMIT = 2;
function loadState() {
  return fs.existsSync(STATE_FILE) ? JSON.parse(fs.readFileSync(STATE_FILE, 'utf-8')) : {};
}
function saveState(state) {
  fs.writeFileSync(STATE_FILE, JSON.stringify(state, null, 2));
}

/** 저장된 청크 본문 → 비교용 텍스트 (헤더·예약 링크 줄·공백 제거, 조회수 줄 제거) */
const bodyKey = (text) => {
  const t = stripInjected(text);
  const i = t.startsWith('제목:') ? t.indexOf('\n\n') : -1;
  return (i >= 0 ? t.slice(i + 2) : t).replace(/(^|\n)조회수\n[\d,]+(?=\n|$)/g, '').replace(/\s+/g, '');
};

// ─────────────────────────────────────────────
// 증분 동기화 (ingest.js · ingest_camping.js 공용)
// ─────────────────────────────────────────────
// · 청크 해시 = "실제로 임베딩하는 최종 텍스트"(표 정규화 + 제목/게시일 헤더 적용 후)의 sha256.
//   처리 로직이 바뀌면 영향받는 청크의 해시만 달라져 그 청크만 다시 임베딩된다.
// · 문서 단위 교체: 봇은 형제 청크를 id 오름차순(=원문 순서)으로 잇기 때문에, 청크 일부만 새로 넣으면
//   순서가 깨진다. → 청크가 하나라도 바뀐 문서는 문서 전체를 새로 넣되, 해시가 같은 청크는 기존
//   임베딩을 복사한다(임베딩 API 호출은 새로 생기거나 바뀐 청크만).
// · 교체 순서: 새 행 전부 insert → 성공하면 그때 이전 행 delete. 도중 실패 시 이전 행은 그대로 남고,
//   다음 실행은 이미 들어간 새 행을 '변경 없음'으로 보므로 자연스럽게 이어서 적재된다.
export const chunkHash = (text) => crypto.createHash('sha256').update(text).digest('hex').slice(0, 32);

/** 저장된 본문 → 임베딩 입력 복원: 임베딩 뒤에 덧붙인 "…예약 홈페이지: URL" 줄 제거
 *  (patch_reserve_links.js, ingest_camping.js 가 붙이는 줄 — 형식 "<라벨> 홈페이지: http…") */
export function stripInjected(content) {
  return content.split('\n').filter((ln) => !/^\S.* 홈페이지: https?:\/\//.test(ln)).join('\n');
}

/** source 의 모든 행 (페이지 단위) */
export async function fetchSourceRows(supabase, source, cols = 'id, metadata') {
  const out = [];
  for (let from = 0; ; from += 1000) {
    const { data, error } = await supabase.from('documents').select(cols)
      .eq('metadata->>source', source).order('id', { ascending: true }).range(from, from + 999);
    if (error) throw new Error(`기존 행 조회 실패: ${error.message}`);
    out.push(...data);
    if (data.length < 1000) return out;
  }
}

const SAME_META = ['doc_type', 'category', 'title', 'url', 'date'];
const CLASS_ORDER = ['new', 'content', 'included', 'views', 'link'];
export const CLASS_LABEL = {
  new: '새 글', content: '내용 수정', views: '조회수만(본문 동일)', link: '링크·메타만',
  included: '새로 포함(예전 길이 기준 제외분)', null: '원래 NULL',
};

async function pagedIds(supabase, source, build) {
  const out = [];
  for (let from = 0; ; from += 1000) {
    const { data, error } = await build(supabase.from('documents').select('id')
      .eq('metadata->>source', source)).order('id', { ascending: true }).range(from, from + 999);
    if (error) throw new Error(`행 조회 실패: ${error.message}`);
    out.push(...data.map((r) => r.id));
    if (data.length < 1000) return out;
  }
}

async function selectByIds(supabase, ids, cols) {
  const out = [];
  for (let i = 0; i < ids.length; i += 100) {
    const { data, error } = await supabase.from('documents').select(cols).in('id', ids.slice(i, i + 100));
    if (error) throw new Error(`행 조회 실패: ${error.message}`);
    out.push(...data);
  }
  return out;
}

/**
 * items: [{ doc, docId, chunkIndex, embedInput, content, metadata }] — 이번 실행이 만든 전체 청크(문서 순서대로)
 * seen : Map(docId → 'loaded' | 제외 사유) — 이번 수집 결과에 '있던' 모든 문서. 여기 없는 기존 문서 = 수집에서 안 보임.
 * 옵션: reembedAll(전부 다시 임베딩), skipEmbed(임베딩 없이 적재), dryRun(DB 변경 없이 계획만)
 *
 * 규칙
 *  · 문서 분류: 새 글 / 내용 수정 / 조회수만(본문 동일) / 링크·메타만 / 새로 포함 / 변경 없음
 *  · 예산(오늘 남은 임베딩 수)은 새 글 → 내용 수정 → 조회수만 → 링크만 → 새로 포함 → 원래 NULL 순으로 쓴다.
 *  · 임베딩이 있던 청크가 NULL 로 바뀌는 교체는 하지 않는다: 교체 후 임베딩 있는 행 수가 교체 전보다
 *    줄어드는 문서는 이번엔 교체하지 않고 기존 행을 둔다(다음 실행에서 다시 시도).
 *    조회수만·링크만 바뀐 문서는 새로 생긴 청크를 모두 임베딩할 수 있을 때만 교체한다.
 *  · 삭제: 수집엔 있으나 필터·제외 목록으로 빠진 문서는 바로, 수집에서 안 보인 문서는 두 번 연속일 때만.
 *  · 🛑 OCR 문서나 '고객만족도'가 DB 에서 빠지거나 청크가 줄면 실제 적재를 거부한다(ALLOW_OCR_LOSS=true 로 해제).
 */
export async function syncSource({ supabase, source, items, seen, embed, pace, batchSize,
  reembedAll = false, skipEmbed = false, dryRun = false }) {
  const runId = new Date().toISOString();
  for (const it of items) it.hash = chunkHash(it.embedInput);
  const aliases = loadAliases(source);
  const keyOf = (docId) => aliases.get(docId) ?? docId;

  const existing = await fetchSourceRows(supabase, source);
  const embeddedIds = new Set(await pagedIds(supabase, source, (q) => q.not('embedding', 'is', null)));
  const oldByKey = new Map();
  for (const r of existing) {
    const k = keyOf(r.metadata?.doc_id);
    if (!oldByKey.has(k)) oldByKey.set(k, []);
    oldByKey.get(k).push(r);
  }
  for (const g of oldByKey.values()) g.sort((a, b) => (a.metadata.chunk_index ?? 0) - (b.metadata.chunk_index ?? 0) || a.id - b.id);
  const newByKey = new Map();
  for (const it of items) {
    if (!newByKey.has(it.docId)) newByKey.set(it.docId, []);
    newByKey.get(it.docId).push(it);
  }
  const hashHasEmb = new Set(existing.filter((r) => embeddedIds.has(r.id)).map((r) => r.metadata?.chunk_hash).filter(Boolean));
  const hashExists = new Set(existing.map((r) => r.metadata?.chunk_hash).filter(Boolean));

  const stats = {
    docs: newByKey.size, unchanged: 0, cls: Object.fromEntries(CLASS_ORDER.map((c) => [c, 0])),
    deferred: [], deleteNow: [], keptMissing: [], merged: [], alerts: [],
    fund: Object.fromEntries(['new', 'content', 'null', 'included', 'views', 'link'].map((c) => [c, [0, 0]])),
    unhashed: existing.filter((r) => !r.metadata?.chunk_hash).length,
  };

  // 1) 문서 분류
  const rewrites = [];   // { key, news, olds, cls }
  const pending = [];
  for (const [key, news] of newByKey) {
    const olds = oldByKey.get(key);
    const sameIds = olds?.every((r) => r.metadata.doc_id === key);
    const same = !reembedAll && olds && sameIds && olds.length === news.length && news.every((it, i) =>
      olds[i].metadata.chunk_hash === it.hash && olds[i].metadata.chunk_index === it.chunkIndex &&
      SAME_META.every((f) => (olds[i].metadata[f] ?? null) === (it.metadata[f] ?? null)));
    if (same) { stats.unchanged++; continue; }
    const rw = { key, news, olds: olds ?? [], cls: null };
    if (!olds) rw.cls = news[0].doc?.oversize ? 'included' : 'new';
    else if (olds.length === news.length && news.every((it, i) => olds[i].metadata.chunk_hash === it.hash)) rw.cls = 'link';
    else pending.push(rw);
    rewrites.push(rw);
  }
  if (pending.length) {
    const rows = await selectByIds(supabase, pending.flatMap((rw) => rw.olds.map((r) => r.id)), 'id, content');
    const text = new Map(rows.map((r) => [r.id, r.content]));
    for (const rw of pending) {
      const oldKey = rw.olds.map((r) => bodyKey(text.get(r.id) ?? '')).join('');
      const newKey = rw.news.map((it) => bodyKey(it.embedInput)).join('');
      rw.cls = oldKey === newKey ? 'views' : 'content';
    }
  }
  for (const rw of rewrites) {
    stats.cls[rw.cls]++;
    const copies = [...new Set(rw.olds.map((r) => r.metadata.doc_id))].filter((d) => d !== rw.key);
    if (copies.length) stats.merged.push({ key: rw.key, title: rw.news[0].metadata.title, copies });
  }

  // 2) 예산 계획 — 우선순위: 새 글 → 내용 수정 → 원래 NULL(백필·새로 포함) → 조회수만 → 링크만
  //    NULL 청크는 벡터 검색에서 아예 안 보이므로, 임베딩이 살아 있는 '조회수만' 문서보다 먼저 채운다.
  //    교체는 회귀 없는 문서만(교체 후 임베딩 있는 행 수 ≥ 교체 전).
  let budget = skipEmbed ? 0 : budgetLeft();
  const funded = new Set();
  const fundOrder = [];   // [hash, cls]
  const fundOne = (h, cls) => { funded.add(h); fundOrder.push([h, cls]); budget--; };
  const covered = (h) => !reembedAll && hashHasEmb.has(h);
  const rank = (c) => CLASS_ORDER.indexOf(c);
  rewrites.sort((a, b) => rank(a.cls) - rank(b.cls));
  const accepted = [];
  const tryDoc = (rw) => {
    const hashes = [...new Set(rw.news.map((it) => it.hash))];
    const textNeeds = hashes.filter((h) => !covered(h) && !funded.has(h) && (reembedAll || !hashExists.has(h)));
    const before = rw.olds.filter((r) => embeddedIds.has(r.id)).length;
    rw.before = before;
    if (rw.cls === 'included') {          // 새 문서(회귀 없음) — 임베딩은 NULL 단계에서 채운다
      stats.fund.included[1] += textNeeds.length;
      accepted.push(rw);
      return;
    }
    const take = textNeeds.slice(0, Math.max(0, budget));
    stats.fund[rw.cls][1] += textNeeds.length;
    const after = rw.news.filter((it) => covered(it.hash) || funded.has(it.hash) || take.includes(it.hash)).length;
    const allFunded = take.length === textNeeds.length;
    // 새 청크가 전부 임베딩을 갖게 되면(NULL 행 없음) 청크 수가 줄어도 회귀가 아니다 — 본문이 짧아진 문서(예: 보정으로
    // 청크 경계가 바뀐 컨벤션센터 6→5)가 '임베딩 행 감소'로 영영 보류되던 문제(2026-11-10)
    const fullyEmbedded = after === rw.news.length;
    const ok = reembedAll || ((after >= before || fullyEmbedded) && (!['views', 'link'].includes(rw.cls) || allFunded));
    if (!ok) {
      stats.deferred.push({ key: rw.key, title: rw.news[0].metadata.title, cls: rw.cls, need: textNeeds.length, before, after });
      return;
    }
    for (const h of take) fundOne(h, rw.cls);
    stats.fund[rw.cls][0] += take.length;
    accepted.push(rw);
  };
  for (const rw of rewrites) if (['new', 'content', 'included'].includes(rw.cls)) tryDoc(rw);

  // 3) 삭제 판정
  const state = loadState();
  const miss = (state[source] ??= {});
  const toDeleteDocs = [];
  for (const [key, olds] of oldByKey) {
    if (newByKey.has(key)) continue;
    const title = olds[0].metadata.title;
    const reason = seen?.get(key);
    if (reason && reason !== 'loaded') {
      stats.deleteNow.push({ key, title, chunks: olds.length, reason });
      toDeleteDocs.push(olds);
    } else {
      const n = (miss[key]?.misses ?? 0) + 1;
      if (n >= MISS_LIMIT) {
        stats.deleteNow.push({ key, title, chunks: olds.length, reason: `수집에서 ${n}회 연속 안 보임` });
        toDeleteDocs.push(olds);
      } else stats.keptMissing.push({ key, title, chunks: olds.length, misses: n });
    }
  }

  // 3-1) 원래 NULL 단계: 교체 문서의 NULL 청크·새로 포함 문서 → 변경 없는(그대로 남는) 문서의 기존 NULL 행 백필
  for (const rw of accepted) {
    for (const h of new Set(rw.news.map((it) => it.hash))) {
      if (covered(h) || funded.has(h)) continue;
      const cls = rw.cls === 'included' ? 'included' : 'null';
      if (cls === 'null') stats.fund.null[1]++;
      if (budget > 0) { fundOne(h, cls); stats.fund[cls][0]++; }
    }
  }
  const rewritten = new Set(rewrites.map((rw) => rw.key));
  const deleting = new Set(toDeleteDocs.flat().map((r) => r.id));
  const backfillRows = new Map();   // hash → 그대로 남는 행 중 임베딩 NULL 인 행 id 들 (insert 없이 embedding 만 update)
  for (const r of existing) {
    const k = keyOf(r.metadata?.doc_id);
    const h = r.metadata?.chunk_hash;
    if (!h || embeddedIds.has(r.id) || rewritten.has(k) || deleting.has(r.id)) continue;
    if (!backfillRows.has(h)) backfillRows.set(h, []);
    backfillRows.get(h).push(r.id);
  }
  for (const h of backfillRows.keys()) {
    if (funded.has(h)) continue;
    stats.fund.null[1]++;
    if (budget > 0) { fundOne(h, 'null'); stats.fund.null[0]++; }
  }
  // 3-2) 조회수만 → 링크만 (남은 예산으로, 새 청크를 모두 임베딩할 수 있을 때만 교체)
  for (const rw of rewrites) if (['views', 'link'].includes(rw.cls)) tryDoc(rw);

  // 4) 🛑 검사: DB 에서 빠지는 문서 전체 + 청크가 줄어드는 교체 문서
  const shrink = accepted.filter((rw) => rw.news.length < rw.olds.length);
  const watchIds = [...toDeleteDocs.flat(), ...shrink.flatMap((rw) => rw.olds)].map((r) => r.id);
  const watchText = new Map((await selectByIds(supabase, watchIds, 'id, content')).map((r) => [r.id, r.content]));
  const sensitive = (olds) => olds.some((r) => (watchText.get(r.id) ?? '').includes('[이미지:') || /고객만족도/.test(r.metadata.title ?? ''));
  for (const olds of toDeleteDocs) if (sensitive(olds)) stats.alerts.push(`삭제: ${olds[0].metadata.title} (${olds.length}청크)`);
  for (const rw of shrink) if (sensitive(rw.olds)) stats.alerts.push(`청크 감소: ${rw.news[0].metadata.title} ${rw.olds.length}→${rw.news.length}`);

  stats.accepted = accepted.length;
  stats.toInsert = accepted.reduce((s, rw) => s + rw.news.length, 0);
  stats.toDelete = accepted.reduce((s, rw) => s + rw.olds.length, 0) + toDeleteDocs.reduce((s, o) => s + o.length, 0);
  stats.plannedEmbed = fundOrder.length;
  stats.budgetLeft = skipEmbed ? 0 : budgetLeft();
  stats.nullInserted = accepted.reduce((s, rw) => s + rw.news.filter((it) => !covered(it.hash) && !funded.has(it.hash)).length, 0);
  if (dryRun) return stats;
  if (stats.alerts.length && process.env.ALLOW_OCR_LOSS !== 'true') {
    throw new Error(`🛑 OCR 문서·고객만족도 손실 — 적재 중단 (확인 후 ALLOW_OCR_LOSS=true): ${stats.alerts.join(' / ')}`);
  }

  // 5) 임베딩 (계획 순서대로). 한도·예산에 걸리면 멈추고, 아래 6) 에서 회귀 문서를 다시 걸러낸다.
  const vec = new Map();
  const textOf = new Map(items.map((it) => [it.hash, it.embedInput]));
  // 백필 대상(그대로 남는 행)의 임베딩 텍스트 = 저장 본문에서 덧붙인 예약 링크 줄 제거
  const bfIds = fundOrder.filter(([h]) => !textOf.has(h) && backfillRows.has(h)).map(([h]) => backfillRows.get(h)[0]);
  for (const r of await selectByIds(supabase, bfIds, 'id, content, metadata->chunk_hash')) textOf.set(r.chunk_hash, stripInjected(r.content));
  let done = 0;
  while (done < fundOrder.length && !skipEmbed) {
    const hs = fundOrder.slice(done, done + batchSize).map(([h]) => h);
    try {
      const vs = await embed(hs.map((h) => textOf.get(h)));
      hs.forEach((h, j) => vec.set(h, vs[j]));
    } catch (err) {
      if (!(err instanceof EmbedQuotaError)) throw err;
      console.warn(`⏸️ ${err.message} — 임베딩 ${done}/${fundOrder.length}에서 중단`);
      break;
    }
    done += hs.length;
    console.log(`🔢 임베딩: ${done}/${fundOrder.length}`);
    if (done < fundOrder.length) await pace();
  }
  stats.embedded = done;
  // 기존 임베딩 재사용 벡터
  const needReuse = new Set(accepted.flatMap((rw) => rw.news.map((it) => it.hash)).filter((h) => covered(h)));
  const reuseIds = [];
  const picked = new Set();
  for (const r of existing) {
    const h = r.metadata?.chunk_hash;
    if (needReuse.has(h) && embeddedIds.has(r.id) && !picked.has(h)) { picked.add(h); reuseIds.push(r.id); }
  }
  for (const r of await selectByIds(supabase, reuseIds, 'id, embedding, metadata->chunk_hash')) vec.set(r.chunk_hash, r.embedding);

  // 6) 실제 결과로 회귀 재검사 — 임베딩이 계획보다 덜 됐으면 그 문서는 교체하지 않는다
  const final = [];
  for (const rw of accepted) {
    const after = rw.news.filter((it) => vec.has(it.hash)).length;
    const newTextOk = !['views', 'link'].includes(rw.cls) || rw.news.every((it) => vec.has(it.hash) || hashExists.has(it.hash));
    // 새 청크가 모두 벡터를 받았으면 청크 수가 줄어도 교체한다(위 계획 단계의 fullyEmbedded 와 같은 기준)
    if (reembedAll || ((after >= rw.before || after === rw.news.length) && newTextOk)) final.push(rw);
    else stats.deferred.push({ key: rw.key, title: rw.news[0].metadata.title, cls: rw.cls, need: 0, before: rw.before, after, late: true });
  }

  // 7) 새 행 insert → 모두 성공하면 이전 행·삭제 대상 delete
  const rowsIn = final.flatMap((rw) => rw.news).map((it) => ({
    content: it.content,
    embedding: vec.get(it.hash) ?? null,
    metadata: { ...it.metadata, chunk_index: it.chunkIndex, chunk_hash: it.hash, run_id: runId, inserted_at: runId },
  }));
  stats.inserted = 0;
  for (let i = 0; i < rowsIn.length; i += batchSize) {
    const { error } = await supabase.from('documents').insert(rowsIn.slice(i, i + batchSize));
    if (error) throw new Error(`insert 실패(${i}~) — 이전 행은 삭제하지 않고 중단: ${error.message}`);
    stats.inserted += Math.min(batchSize, rowsIn.length - i);
  }
  // 백필: 그대로 남는 NULL 행에 임베딩만 채움
  stats.backfilled = 0;
  for (const [h, ids] of backfillRows) {
    if (!vec.has(h)) continue;
    const { error } = await supabase.from('documents').update({ embedding: vec.get(h) }).in('id', ids);
    if (error) throw new Error(`백필 update 실패: ${error.message}`);
    stats.backfilled += ids.length;
  }
  const obsolete = [...final.flatMap((rw) => rw.olds), ...toDeleteDocs.flat()].map((r) => r.id);
  stats.deleted = 0;
  for (let i = 0; i < obsolete.length; i += 200) {
    const { error } = await supabase.from('documents').delete().in('id', obsolete.slice(i, i + 200));
    if (error) throw new Error(`이전 행 삭제 실패: ${error.message}`);
    stats.deleted += Math.min(200, obsolete.length - i);
  }

  // 삭제 기록 — 무엇을 왜 지웠는지 남긴다 (되돌릴 땐 원본 jsonl 에서 다시 적재)
  if (stats.deleteNow.length) {
    fs.mkdirSync('logs', { recursive: true });
    const f = `logs/ingest_deleted_${source.replace(/\W+/g, '_')}_${runId.replace(/[:.]/g, '-')}.json`;
    fs.writeFileSync(f, JSON.stringify({ run_id: runId, source, deleted: stats.deleteNow }, null, 2));
    stats.deleteLog = f;
  }

  // 8) 미발견 기록 갱신
  for (const k of Object.keys(miss)) if (newByKey.has(k) || seen?.has(k)) delete miss[k];
  for (const m of stats.keptMissing) miss[m.key] = { misses: m.misses, title: m.title, last_missing: runId };
  for (const d of stats.deleteNow) delete miss[d.key];
  saveState(state);
  return stats;
}

export function printSyncStats(source, s, { dryRun = false } = {}) {
  const c = s.cls;
  console.log(`\n[${source}] 문서 ${s.docs} — ${Object.entries(c).map(([k, v]) => `${CLASS_LABEL[k]} ${v}`).join(' · ')} · 변경 없음 ${s.unchanged}`);
  console.log(`  교체 ${s.accepted}문서 · 보류(기존 행 유지) ${s.deferred.length}문서 · 삭제 ${s.deleteNow.length}문서 · 미발견 유지 ${s.keptMissing.length}문서`);
  console.log(`  임베딩 계획 ${s.plannedEmbed} (남은 예산 ${s.budgetLeft}) — ` +
    Object.entries(s.fund).map(([k, [f, t]]) => `${CLASS_LABEL[k]} ${f}/${t}`).join(' · '));
  console.log(`  청크: insert ${s.inserted ?? s.toInsert}${dryRun ? '(예정)' : ''} · 이전 행 삭제 ${s.deleted ?? s.toDelete}${dryRun ? '(예정)' : ''} · 그중 NULL 로 들어갈 청크 ${s.nullInserted} (원래 NULL 이던 것, 백필 몫)`);
  if (!dryRun) console.log(`  그대로 남는 행 백필(임베딩만 채움) ${s.backfilled}행${s.deleteLog ? ` · 삭제 로그 ${s.deleteLog}` : ''}`);
  const regress = s.deferred.filter((d) => d.late).length;
  console.log(`  기존 임베딩 → NULL 회귀: 0건 보장 (보류로 막은 문서 ${s.deferred.length}${regress ? `, 그중 실행 중 재검사 ${regress}` : ''})`);
  if (s.deleteNow.length) {
    console.log('  삭제 문서:');
    for (const d of s.deleteNow) console.log(`    - ${d.title} (${d.key}, ${d.chunks}청크) — ${d.reason}`);
  }
  if (s.keptMissing.length) {
    console.log(`  수집에서 안 보였지만 유지 (${MISS_LIMIT}회 연속이면 삭제):`);
    for (const d of s.keptMissing) console.log(`    - ${d.title} (${d.key}, ${d.chunks}청크) — ${d.misses}회째`);
  }
  if (s.merged.length) console.log(`  고정 키로 합쳐지는 사본: ${s.merged.map((m) => `${m.title}(${m.copies.length})`).join(', ')}`);
  if (s.deferred.length) {
    console.log('  보류 문서(예산 부족 — 기존 행·임베딩 유지):');
    for (const d of s.deferred.slice(0, 15)) console.log(`    - [${CLASS_LABEL[d.cls]}] ${d.title} (새 청크 ${d.need}, 임베딩 ${d.before}→${d.after})`);
    if (s.deferred.length > 15) console.log(`    … 외 ${s.deferred.length - 15}건`);
  }
  console.log(s.alerts.length ? `  🛑 OCR 문서·고객만족도 손실: ${s.alerts.join(' / ')}` : '  ✅ DB 에서 빠지거나 청크가 줄어드는 문서 중 OCR 문서·고객만족도 없음');
  console.log(`  오늘(태평양 ${pacificDate()}) 로컬 임베딩 사용 ${usedToday()} · 남은 예산 ${budgetLeft()}`);
  if (s.unhashed) console.log(`  ⚠️ 해시 없는 기존 행 ${s.unhashed}개 — 해시 비교가 안 돼 변경으로 처리되고 다시 임베딩됨 (청크 해시 도입 전 행)`);
}
