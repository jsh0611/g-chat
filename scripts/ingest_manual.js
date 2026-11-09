// 부서 확정 표준답변(data/manual_faq.json 의 faqs)을 documents 테이블에 적재한다.
//
// 홈페이지(크롤링)에 없는 내용을 담당 부서가 직접 준 답으로 보충한다 — 2026-11 시범운영 피드백 FAQ 등.
// metadata.source='manual_faq' 행만 다룬다(공단 문서·예약시스템·관광 행은 건드리지 않음).
// 증분 적재(ingest_utils.syncSource): 바뀐 항목만 다시 임베딩하고, 파일에서 지운 항목은 DB에서도 지운다.
//   (파일에 '있던' 항목만 seen 에 들어가므로 지운 항목은 '수집에서 안 보임'으로 2회 실행 뒤 삭제된다)
// 봇 재랭킹 호환: doc_type 'facility'(게시일 없는 상시 정보 가중치), 제목은 "[시설] 질문".
// 같은 파일의 corrections·supersede 는 공단 문서용이라 scripts/ingest.js 가 적용한다.
//
// 실행: node scripts/ingest_manual.js   (DRY_RUN=true 면 DB 변경 없이 변경 통계만)
import { createClient } from '@supabase/supabase-js';
import fs from 'fs';
import dotenv from 'dotenv';
import { pathToFileURL } from 'url';
import { chunkText, withHeader, syncSource, printSyncStats } from '../lib/ingest_utils.js';
import { embedBatch, pace, EMBED_BATCH_SIZE } from '../lib/embed_utils.js';

dotenv.config();

const SOURCE_FILE = 'data/manual_faq.json';
const SOURCE = 'manual_faq';
const DRY_RUN = process.env.DRY_RUN === 'true';
const REEMBED_ALL = process.env.REEMBED_ALL === 'true';
const SKIP_EMBED = process.env.SKIP_EMBED === 'true';

export function buildItems(file = SOURCE_FILE) {
  const { faqs = [] } = JSON.parse(fs.readFileSync(file, 'utf-8'));
  const docs = [];
  const items = [];
  const seen = new Map();
  for (const f of faqs) {
    const docId = `manual-${f.id}`;
    const doc = {
      docId,
      docType: 'facility',
      category: f.dept ?? '',
      title: `[${f.facility}] ${f.question}`,
      url: '',
      date: null,
      content: `시설: ${f.facility}\n질문: ${f.question}\n답변: ${f.answer}\n담당: ${f.dept ?? ''}`,
    };
    seen.set(docId, 'loaded');
    docs.push(doc);
    const chunks = chunkText(doc.content);
    for (let c = 0; c < chunks.length; c++) {
      const embedInput = withHeader(doc, chunks[c].trim());
      items.push({
        doc, docId, chunkIndex: c, embedInput, content: embedInput,
        metadata: {
          source: SOURCE, doc_id: docId, doc_type: doc.docType, category: doc.category,
          title: doc.title, url: doc.url, date: doc.date,
        },
      });
    }
  }
  return { docs, items, seen };
}

async function main() {
  const { docs, items, seen } = buildItems(process.env.INGEST_FILE || SOURCE_FILE);
  console.log(`📄 ${SOURCE} 문서 ${docs.length}개 → 🧩 청크 ${items.length}개`);
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
