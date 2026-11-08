import { createClient } from '@supabase/supabase-js';
import fs from 'fs';
import dotenv from 'dotenv';
import { pathToFileURL } from 'url';
import {
  normalizeTables, normalizeDate, chunkText, withHeader, loadExclusions, syncSource, printSyncStats,
  isLedger, capAttachments, keepLatestVersions,
} from '../lib/ingest_utils.js';
import { embedBatch, pace, EMBED_BATCH_SIZE } from '../lib/embed_utils.js';

// .env 파일 로드
dotenv.config();

// 1. Supabase / 임베딩 클라이언트 초기화
// RLS(보안)를 우회하기 위해 반드시 SERVICE_ROLE_KEY를 사용해야 합니다.
// 임베딩: Gemini gemini-embedding-001 (embed_utils.js 참고 — 1536차원, 무료 티어 분당 한도로 배치 50개/분)
const supabase = createClient(
  process.env.SUPABASE_URL,
  process.env.SUPABASE_SERVICE_ROLE_KEY
);

// ─────────────────────────────────────────────
// 설정
// ─────────────────────────────────────────────
const SOURCE_FILE = 'documents.jsonl';
// 임베딩 API 한 번에 보낼 청크 수
const BATCH_SIZE = EMBED_BATCH_SIZE;
// 공지사항·채용공고·행사안내만 날짜 제한. FAQ·규정 등 다른 게시판은 작성일과 무관하게 유지.
const BOARD_DATE_CUTOFF = '2025-01-01';
const DATED_BOARD_CATEGORIES = new Set(['공지사항', '채용공고', '행사안내']);
// 적재하지 않는 게시판 — 칭찬합시다는 시민 실명·직원 실명 위주라 안내에 쓸 내용이 없다 (크롤링은 그대로, 적재만 제외)
const EXCLUDED_BOARD_CATEGORIES = new Set(['칭찬합시다']);
// true면 DB에 쓰지 않고, 지금 DB와 비교한 변경 통계(다시 임베딩할 청크 수 등)와 샘플만 출력
const DRY_RUN = process.env.DRY_RUN === 'true';
// 증분 적재(ingest_utils.syncSource): 청크 해시가 같은 행은 그대로 두고, 새로 생기거나 바뀐 청크만 임베딩한다.
//   중단돼도 다음 실행이 이미 들어간 새 행을 '변경 없음'으로 보므로 그대로 다시 실행하면 이어서 적재된다
//   (예전 RESUME_FROM 은 더 이상 필요 없음).
// REEMBED_ALL=true면 해시와 무관하게 이 출처 전체를 다시 임베딩 (임베딩 모델을 바꿀 때)
const REEMBED_ALL = process.env.REEMBED_ALL === 'true';
// SKIP_EMBED=true면 임베딩 없이 본문만 적재 (Gemini 무료 티어 한도 때문에 전체 적재가 오래 걸릴 때 사용)
// → 키워드 검색은 즉시 동작하고, 임베딩은 embed_backfill.js로 나중에 채운다
const SKIP_EMBED = process.env.SKIP_EMBED === 'true';
// 긴 문서: 대장류(정보목록 등)는 제목으로 거르고, 그 밖의 문서는 첨부 하나당 앞 3만 자만 넣는다
//   (ingest_utils.isLedger / capAttachments). 예전 '10만 자 넘으면 문서 통째로 스킵' 은 폐지 —
//   갱신된 일반 문서(고객만족도 등)까지 빠지는 문제가 있었다.
const OVERSIZE = 100000;   // 예전 기준 — 이번에 처음 들어오는 문서를 '새로 포함'으로 구분하는 데만 씀

/**
 * 2. documents.jsonl을 문서 단위로 파싱
 * 형식(줄당 1개 JSON):
 *   { "id": "...", "content": "...",
 *     "metadata": { "doc_id", "source_type"(board|facility|page), "title", "url", "date", "source" } }
 *
 * 새 크롤러의 표는 마크다운(|a|b|)이 아니라 바깥 파이프 없는 "a | b | c" 줄이므로,
 * 파이프를 감싸 마크다운 형태로 만든 뒤 기존 restructureTables()로 레코드 문장 변환한다.
 * 반환 seen: 이 파일에 있던 모든 문서 → 'loaded' 또는 제외 사유 (삭제 판정용)
 */
function parseDocuments(raw) {
  const docs = [];
  const seen = new Map();
  const cutDocs = [];
  // 중복 제거 적용 목록(dedup_exclusions.json, 없으면 빈 집합) — 원본 jsonl 은 그대로 두고 적재만 건너뜀
  const excluded = loadExclusions('dhsisul');
  for (const line of raw.split('\n')) {
    if (!line.trim()) continue;
    let obj;
    try {
      obj = JSON.parse(line);
    } catch (e) {
      console.warn('⚠️ JSON 파싱 실패한 줄 스킵:', line.slice(0, 80));
      continue;
    }
    const meta = obj.metadata ?? {};
    const docId = meta.doc_id ?? obj.id;
    if (excluded.has(docId)) { seen.set(docId, '중복 제거 제외 목록'); continue; }
    if (isLedger(meta.title)) { seen.set(docId, '대장류(제목)'); continue; }
    if (meta.source_type === 'board' && EXCLUDED_BOARD_CATEGORIES.has(meta.source)) { seen.set(docId, '제외 게시판'); continue; }
    // 기간성 게시판만 옛 글 제외 (FAQ·규정 등은 날짜와 무관하게 유지)
    const docDate = normalizeDate(meta.date);
    if (meta.source_type === 'board' && DATED_BOARD_CATEGORIES.has(meta.source) && docDate && docDate < BOARD_DATE_CUTOFF) { seen.set(docId, '게시일 컷'); continue; }
    const rawLen = (obj.content ?? '').length;
    const { content: current, dropped } = keepLatestVersions(meta.title, obj.content ?? '');
    const { content: capped, cuts } = capAttachments(current);
    if (cuts.length || dropped.length) cutDocs.push({ title: meta.title, docId, from: rawLen, to: capped.length, cuts, dropped });
    const content = normalizeTables(capped);
    if (!content.trim()) { seen.set(docId, '본문 없음'); continue; }
    seen.set(docId, 'loaded');

    docs.push({
      docId,
      // 봇의 재랭킹 로직(doc_type === 'facility')과 호환되도록 source_type을 doc_type으로 사용
      docType: meta.source_type ?? '',
      category: meta.source ?? '',
      title: meta.title ?? '',
      url: meta.url ?? '',
      date: normalizeDate(meta.date), // 없으면 null (facility/page는 게시일 없음)
      oversize: rawLen > OVERSIZE,
      content,
    });
  }
  return { docs, seen, cutDocs };
}

// normalizeTables · normalizeDate · chunkText · withHeader · syncSource 는 ingest_utils.js (ingest_camping.js 와 공용)

/** 전체 문서 → 청크 목록 (임베딩 입력 = 표 정규화 + 제목/게시일 헤더까지 적용된 최종 텍스트).
 *  다른 스크립트가 '지금 로직의 청크'를 만들어 저장된 행과 비교할 때도 이 함수를 쓴다. */
export function buildItems(file = SOURCE_FILE) {
  // 널 문자(\u0000)는 Postgres가 거부하므로 제거 ("unsupported Unicode escape sequence" 방지)
  const rawText = fs.readFileSync(file, 'utf-8').replace(/\u0000/g, '');
  const { docs, seen, cutDocs } = parseDocuments(rawText);
  const items = [];
  for (const doc of docs) {
    const chunks = chunkText(doc.content);
    for (let c = 0; c < chunks.length; c++) {
      const body = chunks[c].trim();
      if (body.length < 10) continue; // 너무 짧은 쓰레기 스킵

      // [핵심] 제목(+게시일)을 본문 앞에 붙여 임베딩 → 제목 기반 검색 정확도 향상,
      // 게시일은 LLM이 옛 공지를 구분할 수 있게 문맥에도 노출
      const embedInput = withHeader(doc, body);
      items.push({
        doc, docId: doc.docId, chunkIndex: c, embedInput,
        content: embedInput, // 검색 시 제목까지 보이도록 함께 저장
        metadata: {
          source: SOURCE_FILE,
          doc_id: doc.docId,
          doc_type: doc.docType,
          category: doc.category,
          title: doc.title,
          url: doc.url,
          date: doc.date, // 게시일(YYYY-MM-DD), 없으면 null
        },
      });
    }
  }
  return { docs, items, seen, cutDocs };
}

/**
 * 4. 메인 적재 프로세스 (증분)
 */
async function processAndIngest() {
  console.log('🚀 데이터 적재 파이프라인 시작...');
  // INGEST_FILE: 드라이런에서 갓 수집한 파일을 저장소 루트로 복사하기 전에 미리 보기 위한 경로 지정
  const file = process.env.INGEST_FILE || SOURCE_FILE;
  const { docs, items, seen, cutDocs } = buildItems(file);
  console.log(`📄 ${file}: ${docs.length}개 문서 → 🧩 ${items.length}개 청크`);
  const ledgers = [...seen.values()].filter((v) => v === '대장류(제목)').length;
  console.log(`  대장류(제목으로 제외) ${ledgers}건 · 첨부를 자르거나 옛 버전을 뺀 문서 ${cutDocs.length}건`);
  for (const d of cutDocs) {
    console.log(`    ✂️ ${d.title} (${d.docId}) ${d.from.toLocaleString()}자 → ${d.to.toLocaleString()}자 · 잘린 첨부 ${d.cuts.length}개: ` +
      d.cuts.map((c) => `${c.name.slice(0, 24)} ${Math.round(c.from / 1000)}k`).join(', '));
    if (d.dropped.length) console.log(`       옛 버전·서식 첨부 ${d.dropped.length}개 제외(현행만): ${d.dropped.join(', ')}`);
  }

  const stats = await syncSource({
    supabase, source: SOURCE_FILE, items, seen, embed: embedBatch, pace, batchSize: BATCH_SIZE,
    reembedAll: REEMBED_ALL, skipEmbed: SKIP_EMBED, dryRun: DRY_RUN,
  });
  printSyncStats(SOURCE_FILE, stats, { dryRun: DRY_RUN });
  if (!DRY_RUN) console.log('\n🎉 완료!');
}

// 스크립트로 실행될 때만 적재 (다른 스크립트가 buildItems 만 가져다 쓸 수 있게)
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  processAndIngest().catch((err) => {
    console.error('파이프라인 실행 중 치명적 에러 발생:', err);
    process.exit(1);
  });
}
