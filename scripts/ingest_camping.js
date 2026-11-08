// 동해시 관광지 통합예약시스템(campingkorea.or.kr) 문서를 documents 테이블에 적재한다.
//
// 입력: documents_camping.jsonl (data-pipeline: python main.py camping → 저장소 루트로 복사)
// metadata.source='campingkorea' 행만 다룬다 — 공단 문서·관광 POI 행은 건드리지 않는다.
// 증분 적재(ingest_utils.syncSource): 청크 해시가 같은 행은 그대로 두고 새로 생기거나 바뀐 청크만 임베딩,
//   새 행을 모두 넣은 뒤에 이전 행을 지운다(도중 실패 시 이전 행 보존, 재실행하면 이어서 적재).
//   REEMBED_ALL=true 면 이 출처 전체를 다시 임베딩 (임베딩 모델을 바꿀 때).
//
// 봇 재랭킹 호환:
//   · 이용안내·시설·안내전화 = doc_type 'facility' → 게시일 없는 상시 정보 가중치(+0.15, 시설 의도 +0.2)
//   · 공지·FAQ = doc_type 'board' + 게시일 → 제목/게시일 헤더, 최신성 보정
//   · 여행코스 = doc_type 'page' (tour 로 넣으면 ingest_tour.js 재실행 때 함께 지워짐)
// 예약 링크: facility 문서의 청크에 "예약 홈페이지: <온라인예약 URL>" 줄을 붙인다(patch_reserve_links.js 와
//   같은 형식 — 봇 프롬프트가 이 줄만 예약 링크로 쓰고 (안내 페이지: URL)과 구분한다). 온라인예약 페이지는 수집하지 않는다.
//   이 줄은 임베딩 입력(=청크 해시 대상)이 아니라 저장 본문에만 붙는다.
//
// 실행: node scripts/ingest_camping.js   (DRY_RUN=true 면 DB 변경 없이 지금 DB 대비 변경 통계와 샘플만)
import { createClient } from '@supabase/supabase-js';
import fs from 'fs';
import dotenv from 'dotenv';
import { pathToFileURL } from 'url';
import {
  normalizeTables, normalizeDate, chunkText, withHeader, loadExclusions, syncSource, printSyncStats, capAttachments,
} from '../lib/ingest_utils.js';
import { embedBatch, pace, EMBED_BATCH_SIZE } from '../lib/embed_utils.js';

dotenv.config();

const SOURCE_FILE = 'documents_camping.jsonl';
const SOURCE = 'campingkorea';
const DRY_RUN = process.env.DRY_RUN === 'true';
const REEMBED_ALL = process.env.REEMBED_ALL === 'true';
const SKIP_EMBED = process.env.SKIP_EMBED === 'true';
// ingest.js 와 같은 옛 공지 컷. 단, 상단 고정 공지와 FAQ 는 게시일과 무관하게 현재 유효하므로 제외.
const BOARD_DATE_CUTOFF = '2025-01-01';

function parseDocuments(raw, excluded) {
  const docs = [];
  const skipped = { old: 0, excluded: 0 };
  const seen = new Map();   // 이번 파일에 있던 모든 문서 → 'loaded' 또는 제외 사유 (삭제 판정용)
  for (const line of raw.split('\n')) {
    if (!line.trim()) continue;
    const { id, content, metadata: m = {} } = JSON.parse(line);
    const docId = m.doc_id ?? id;
    if (excluded.has(docId)) { skipped.excluded++; seen.set(docId, '중복 제거 제외 목록'); continue; }
    const date = normalizeDate(m.date);
    if (m.kind === 'notice' && !m.pinned && date && date < BOARD_DATE_CUTOFF) { skipped.old++; seen.set(docId, '게시일 컷'); continue; }
    const text = normalizeTables(capAttachments(content ?? '').content);
    if (!text.trim()) { seen.set(docId, '본문 없음'); continue; }
    seen.set(docId, 'loaded');
    docs.push({
      docId: m.doc_id ?? id,
      docType: m.source_type,
      kind: m.kind,
      category: m.category ?? '',
      title: m.title ?? '',
      url: m.url ?? '',
      date,
      reserveUrl: m.source_type === 'facility' ? m.reserve_url : null,
      content: text,
    });
  }
  return { docs, skipped, seen };
}

/** 전체 문서 → 청크 목록 (다른 스크립트가 '지금 로직의 청크'를 만들 때도 이 함수를 쓴다) */
export function buildItems(file = SOURCE_FILE) {
  const raw = fs.readFileSync(file, 'utf-8').replace(/\u0000/g, '');
  const { docs, skipped, seen } = parseDocuments(raw, loadExclusions(SOURCE));
  const items = [];
  for (const doc of docs) {
    const chunks = chunkText(doc.content);
    for (let c = 0; c < chunks.length; c++) {
      const body = chunks[c].trim();
      if (body.length < 10) continue;
      const embedInput = withHeader(doc, body);
      // 예약 링크는 임베딩 입력이 아니라 저장 본문에만 붙인다 (patch_reserve_links.js 와 같은 방식)
      const withLink = doc.reserveUrl && (c === 0 || /예약|취소|환불/.test(body));
      items.push({
        doc, docId: doc.docId, chunkIndex: c, embedInput,
        content: withLink ? `${embedInput}\n예약 홈페이지: ${doc.reserveUrl}` : embedInput,
        metadata: {
          source: SOURCE,
          doc_id: doc.docId,
          doc_type: doc.docType,
          category: doc.category,
          title: doc.title,
          url: doc.url,
          date: doc.date,
        },
      });
    }
  }
  return { docs, items, skipped, seen };
}

async function main() {
  const { docs, items, skipped, seen } = buildItems(process.env.INGEST_FILE || SOURCE_FILE);
  console.log(`📄 ${SOURCE} 문서 ${docs.length}개 (옛 공지 컷 ${skipped.old}, 중복 제외 목록 ${skipped.excluded}) → 🧩 청크 ${items.length}개`);

  if (DRY_RUN) {
    const pick = (re) => items.find((it) => re.test(it.doc.title));
    for (const it of [pick(/취소 및 환불/), pick(/추암오토캠핑장 이용요금/), pick(/^\[/)].filter(Boolean)) {
      console.log('────────────────────────');
      console.log(it.content.slice(0, 1200));
    }
  }

  const supabase = createClient(process.env.SUPABASE_URL, process.env.SUPABASE_SERVICE_ROLE_KEY);
  const stats = await syncSource({
    supabase, source: SOURCE, items, seen, embed: embedBatch, pace, batchSize: EMBED_BATCH_SIZE,
    reembedAll: REEMBED_ALL, skipEmbed: SKIP_EMBED, dryRun: DRY_RUN,
  });
  printSyncStats(SOURCE, stats, { dryRun: DRY_RUN });
  if (!DRY_RUN) console.log('\n🎉 완료!');
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((err) => {
    console.error('치명적 에러:', err);
    process.exit(1);
  });
}
