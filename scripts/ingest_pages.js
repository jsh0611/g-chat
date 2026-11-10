// 시설 전용 사이트의 안내 페이지(data/extra_pages.json)를 받아 documents 테이블에 적재한다.
//
// 공단 본 홈페이지 크롤러(data-pipeline)가 다루지 않는 페이지 — 예: 해오름 테니스장 사이트의 이용시간·요금·환불 안내.
// metadata.source='extra_pages' 행만 다룬다. 증분 적재(ingest_utils.syncSource): 내용이 바뀐 페이지만 다시 임베딩.
// 본문 추출: HTML 주석·script·style 제거 → start~end 구간 → 표는 "a | b" 줄(normalizeTables 로 레코드 문장화).
//   주석으로 숨긴 옛 열·섹션(예: 테니스장 '사용일 1일 전 70%', 사용료 감면 섹션)은 화면에 안 보이므로 넣지 않는다.
// 받기에 실패한 페이지는 seen 에 넣지 않는다 → 한 번 실패로 지워지지 않고, 2회 연속 실패해야 삭제된다.
//
// 실행: node scripts/ingest_pages.js   (DRY_RUN=true 면 DB 변경 없이 변경 통계 + 추출 본문 미리보기)
import { createClient } from '@supabase/supabase-js';
import fs from 'fs';
import dotenv from 'dotenv';
import { pathToFileURL } from 'url';
import { normalizeTables, chunkText, withHeader, syncSource, printSyncStats } from '../lib/ingest_utils.js';
import { embedBatch, pace, EMBED_BATCH_SIZE } from '../lib/embed_utils.js';

dotenv.config({ quiet: true });

const SOURCE_FILE = 'data/extra_pages.json';
const SOURCE = 'extra_pages';
const DRY_RUN = process.env.DRY_RUN === 'true';
const REEMBED_ALL = process.env.REEMBED_ALL === 'true';
const SKIP_EMBED = process.env.SKIP_EMBED === 'true';

const ENTITIES = { nbsp: ' ', amp: '&', lt: '<', gt: '>', quot: '"', '#39': "'", apos: "'" };
const decodeEntities = (s) => s
  .replace(/&#(\d+);/g, (_, n) => String.fromCodePoint(Number(n)))
  .replace(/&#x([0-9a-f]+);/gi, (_, n) => String.fromCodePoint(parseInt(n, 16)))
  .replace(/&(nbsp|amp|lt|gt|quot|#39|apos);/g, (_, e) => ENTITIES[e]);

/** 안내 페이지 HTML → 화면에 보이는 본문 텍스트 (표는 "a | b" 줄) */
export function extractText(html, { start, end } = {}) {
  let s = html.replace(/<!--[\s\S]*?-->/g, '').replace(/<(script|style|noscript)[^>]*>[\s\S]*?<\/\1>/gi, '');
  if (start) { const i = s.indexOf(start); if (i >= 0) s = s.slice(i); }
  if (end) { const j = s.indexOf(end); if (j >= 0) s = s.slice(0, j); }
  s = s
    // 표 셀 안 줄바꿈("동절기<br>(11월 ~ 익년 2월)")은 공백 — 줄로 바꾸면 머리글 행이 쪼개진다
    .replace(/<table[\s\S]*?<\/table>/gi, (t) => t.replace(/<br\s*\/?>/gi, ' '))
    .replace(/<br\s*\/?>/gi, '\n')
    .replace(/<\/t[hd]>\s*(?=<t[hd][\s>])/gi, ' | ')   // 같은 행의 셀 사이
    .replace(/<\/(p|div|li|tr|h\d|table|caption|thead|tbody|ul|ol)>/gi, '\n')
    .replace(/<li[^>]*>/gi, '- ')
    .replace(/<[^>]+>/g, '');
  const lines = decodeEntities(s).split('\n')
    .map((l) => l.replace(/[ \t ]+/g, ' ').trim())
    .filter((l) => l && l !== '※');
  return normalizeTables(lines.join('\n'));
}

async function fetchPage(url) {
  const res = await fetch(url, { headers: { 'User-Agent': 'Mozilla/5.0 (g-chat ingest)' } });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const buf = new Uint8Array(await res.arrayBuffer());
  try { return new TextDecoder('utf-8', { fatal: true }).decode(buf); } catch { return new TextDecoder('euc-kr').decode(buf); }
}

export async function buildItems(file = SOURCE_FILE) {
  const { pages = [] } = JSON.parse(fs.readFileSync(file, 'utf-8'));
  const docs = [];
  const items = [];
  const seen = new Map();
  for (const p of pages) {
    const docId = `page-${p.id}`;
    let content;
    try {
      content = extractText(await fetchPage(p.url), p);
    } catch (err) {
      console.warn(`⚠️ ${p.url} 받기 실패(${err.message}) — 이번엔 건너뜀(기존 행 유지)`);
      continue;
    }
    if (content.length < 50) { console.warn(`⚠️ ${p.url} 본문이 너무 짧음(${content.length}자) — start/end 확인`); continue; }
    const doc = { docId, docType: 'facility', category: p.category ?? '', title: p.title, url: p.url, date: null, content };
    seen.set(docId, 'loaded');
    docs.push(doc);
    const chunks = chunkText(content);
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
  const { docs, items, seen } = await buildItems(process.env.INGEST_FILE || SOURCE_FILE);
  console.log(`📄 ${SOURCE} 문서 ${docs.length}개 → 🧩 청크 ${items.length}개`);
  if (DRY_RUN) for (const d of docs) console.log(`\n── ${d.title} (${d.content.length}자)\n${d.content}`);
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
