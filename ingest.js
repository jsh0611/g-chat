import { createClient } from '@supabase/supabase-js';
import OpenAI from 'openai';
import fs from 'fs';
import dotenv from 'dotenv';
import { restructureTables } from './table_utils.js';

// .env 파일 로드
dotenv.config();

// 1. Supabase / 임베딩 클라이언트 초기화
// RLS(보안)를 우회하기 위해 반드시 SERVICE_ROLE_KEY를 사용해야 합니다.
// 임베딩: LLM 게이트웨이(factchat)에는 임베딩 API가 없어 전용 OpenAI 키(OPENAI_API_KEY_2) 사용.
//   text-embedding-3-small, 1536차원 (기존 vector(1536) 스키마와 일치, 정규화된 벡터 반환)
const supabase = createClient(
  process.env.SUPABASE_URL,
  process.env.SUPABASE_SERVICE_ROLE_KEY
);

const embedClient = new OpenAI({ apiKey: process.env.OPENAI_API_KEY_2 });

// ─────────────────────────────────────────────
// 설정
// ─────────────────────────────────────────────
const SOURCE_FILE = 'documents.jsonl';
// true면 적재 전에 documents 테이블을 비웁니다.
const RESET_TABLE = true;
// 임베딩 API 한 번에 보낼 청크 수
const BATCH_SIZE = 100;
// board 공지 보관 컷: 이 날짜 이전 게시물은 적재하지 않음 (날짜 없는 board·facility·page는 항상 유지)
const BOARD_DATE_CUTOFF = '2025-01-01';
// true면 DB에 쓰지 않고 파싱/청크 통계와 샘플만 출력 (임베딩 비용 0)
const DRY_RUN = process.env.DRY_RUN === 'true';
// 중단된 적재 이어하기: RESUME_FROM=<청크 인덱스>면 테이블을 비우지 않고 그 지점부터 적재
// (진행 로그의 "진행: X/전체"에서 X를 넣으면 됨)
const RESUME_FROM = parseInt(process.env.RESUME_FROM ?? '0', 10) || 0;
// SKIP_EMBED=true면 임베딩 없이 본문만 적재 (Gemini 무료 티어 일 1,000건 한도 소진 시 사용)
// → 키워드 검색은 즉시 동작하고, 임베딩은 embed_backfill.js로 나중에 채운다
const SKIP_EMBED = process.env.SKIP_EMBED === 'true';
// 이 길이를 넘는 문서는 대장류 데이터 덤프(연도별 "정보목록" = 내부 공문 제목 목록 등)로 간주하고 스킵.
// 코퍼스 34M자 중 72%가 정보목록 39건 — 챗봇 답변에 못 쓰는 노이즈이며 DB 용량만 잠식함.
const MAX_DOC_CHARS = 100000;

/**
 * 2. documents.jsonl을 문서 단위로 파싱
 * 형식(줄당 1개 JSON):
 *   { "id": "...", "content": "...",
 *     "metadata": { "doc_id", "source_type"(board|facility|page), "title", "url", "date", "source" } }
 *
 * 새 크롤러의 표는 마크다운(|a|b|)이 아니라 바깥 파이프 없는 "a | b | c" 줄이므로,
 * 파이프를 감싸 마크다운 형태로 만든 뒤 기존 restructureTables()로 레코드 문장 변환한다.
 */
function parseDocuments(raw) {
  const docs = [];
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
    if ((obj.content ?? '').length > MAX_DOC_CHARS) {
      console.log(`⏭️ 대용량 대장류 스킵 (${Math.round(obj.content.length / 1000)}k자): ${meta.title}`);
      continue;
    }
    // 옛 공지 컷: 게시일 있는 board 문서만 대상 (상시 정보는 날짜와 무관하게 유지)
    const docDate = normalizeDate(meta.date);
    if (meta.source_type === 'board' && docDate && docDate < BOARD_DATE_CUTOFF) continue;
    let content = normalizeTables(obj.content ?? '');
    if (!content.trim()) continue;

    docs.push({
      docId: meta.doc_id ?? obj.id,
      // 봇의 재랭킹 로직(doc_type === 'facility')과 호환되도록 source_type을 doc_type으로 사용
      docType: meta.source_type ?? '',
      category: meta.source ?? '',
      title: meta.title ?? '',
      url: meta.url ?? '',
      date: normalizeDate(meta.date), // 없으면 null (facility/page는 게시일 없음)
      content,
    });
  }
  return docs;
}

/**
 * 새 크롤러 표 형식 → 레코드 문장 변환
 *  - "[표]" 마커 줄 제거
 *  - " | " 구분 줄 블록을 마크다운 표(|a|b|)로 감싼 뒤 restructureTables() 적용
 *  - 셀 전체가 같은 값 반복인 줄(colspan 병합 풀림, 예: "14시 수업 | 14시 수업 | ...")은 셀 하나로 축약
 */
function normalizeTables(content) {
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

/** 임베딩 배치 호출 (429 등 일시 오류 대비 재시도 + 백오프) */
async function embedBatch(inputs) {
  for (let attempt = 1; ; attempt++) {
    try {
      const res = await embedClient.embeddings.create({
        model: 'text-embedding-3-small',
        input: inputs,
      });
      return res.data.map((d) => d.embedding); // 각 1536차원, 정규화됨
    } catch (err) {
      if (attempt >= 5) throw err;
      const wait = attempt * 10000;
      console.warn(`⚠️ 임베딩 실패(${(err.message ?? '').slice(0, 120)}) — ${wait / 1000}초 후 재시도 (${attempt}/5)`);
      await new Promise((r) => setTimeout(r, wait));
    }
  }
}

/** 다양한 표기의 날짜를 "YYYY-MM-DD"로 정규화. 해석 불가하면 null */
function normalizeDate(raw) {
  if (!raw) return null;
  const m = raw.match(/(\d{4})\s*[.\-\/년]\s*(\d{1,2})\s*[.\-\/월]\s*(\d{1,2})/);
  if (!m) return null;
  const [, y, mo, d] = m;
  return `${y}-${mo.padStart(2, '0')}-${d.padStart(2, '0')}`;
}

/**
 * 3. 텍스트를 줄 단위로 청크 분할
 * 문자 수로 자르되 줄 중간에서 자르지 않는다 — 표 레코드 한 줄이 반토막 나는 것을 방지.
 * 단일 줄이 chunkSize보다 길면 그 줄만 문자 단위로 분할.
 */
function chunkText(text, chunkSize = 500) {
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

/**
 * 4. 메인 적재 프로세스
 */
async function processAndIngest() {
  console.log('🚀 데이터 적재 파이프라인 시작...');

  try {
    // 널 문자(\u0000)는 Postgres가 거부하므로 제거 ("unsupported Unicode escape sequence" 방지)
    const rawText = fs.readFileSync(SOURCE_FILE, 'utf-8').replace(/\u0000/g, '');
    const docs = parseDocuments(rawText);
    console.log(`📄 ${docs.length}개 문서를 파싱했습니다.`);

    // (선택) 기존 데이터 초기화
    if (RESET_TABLE && !DRY_RUN && RESUME_FROM === 0) {
      console.log('🧹 기존 documents 테이블을 비웁니다...');
      // id > 0 조건으로 전체 삭제 (Supabase는 조건 없는 delete를 막음)
      const { error: delError } = await supabase
        .from('documents')
        .delete()
        .neq('id', 0);
      if (delError) {
        console.error('⚠️ 초기화 실패:', delError.message);
      } else {
        console.log('✅ 테이블 초기화 완료.');
      }
    }

    // 전체 문서 → 청크 목록으로 평탄화
    const items = [];
    for (const doc of docs) {
      const chunks = chunkText(doc.content);
      for (let c = 0; c < chunks.length; c++) {
        const body = chunks[c].trim();
        if (body.length < 10) continue; // 너무 짧은 쓰레기 스킵

        // [핵심] 제목(+게시일)을 본문 앞에 붙여 임베딩 → 제목 기반 검색 정확도 향상,
        // 게시일은 LLM이 옛 공지를 구분할 수 있게 문맥에도 노출
        const header = [
          doc.title ? `제목: ${doc.title}` : null,
          doc.date ? `게시일: ${doc.date}` : null,
        ].filter(Boolean).join('\n');
        const embedInput = header ? `${header}\n\n${body}` : body;
        items.push({ doc, chunkIndex: c, embedInput });
      }
    }
    console.log(`🧩 총 ${items.length}개 청크를 ${Math.ceil(items.length / BATCH_SIZE)}개 배치로 적재합니다.`);

    if (DRY_RUN) {
      const totalChars = items.reduce((s, it) => s + it.embedInput.length, 0);
      const byType = {};
      for (const it of items) byType[it.doc.docType] = (byType[it.doc.docType] ?? 0) + 1;
      console.log(`\n[DRY_RUN] 청크 통계 — 총 문자수 ${totalChars.toLocaleString()}, 타입별:`, byType);
      console.log('\n[DRY_RUN] 샘플 청크 3개:\n');
      for (const it of [items[0], items[Math.floor(items.length / 2)], items[items.length - 1]]) {
        console.log('────────────────────────');
        console.log(it.embedInput.slice(0, 800));
      }
      return;
    }

    let successCount = RESUME_FROM;
    if (RESUME_FROM > 0) console.log(`⏩ ${RESUME_FROM}번째 청크부터 이어서 적재합니다.`);
    for (let start = RESUME_FROM; start < items.length; start += BATCH_SIZE) {
      const batch = items.slice(start, start + BATCH_SIZE);
      const embeddings = SKIP_EMBED ? null : await embedBatch(batch.map((it) => it.embedInput));

      const rows = batch.map((it, i) => ({
        content: it.embedInput, // 검색 시 제목까지 보이도록 함께 저장
        embedding: embeddings ? embeddings[i] : null,
        metadata: {
          source: SOURCE_FILE,
          doc_id: it.doc.docId,
          doc_type: it.doc.docType,
          category: it.doc.category,
          title: it.doc.title,
          url: it.doc.url,
          date: it.doc.date, // 게시일(YYYY-MM-DD), 없으면 null
          chunk_index: it.chunkIndex,
          inserted_at: new Date().toISOString(),
        },
      }));

      const { error } = await supabase.from('documents').insert(rows);
      if (error) {
        console.error(`❌ [배치 ${start / BATCH_SIZE + 1}] 저장 실패:`, error.message);
      } else {
        successCount += rows.length;
      }
      console.log(
        `✅ 진행: ${Math.min(start + BATCH_SIZE, items.length)}/${items.length} (성공 ${successCount})`
      );

      // DB 부하 방지
      await new Promise((resolve) => setTimeout(resolve, 50));
    }

    console.log(
      `\n🎉 완료! 문서 ${docs.length}개 → 청크 ${items.length}개 중 ${successCount}개 적재 성공.`
    );
  } catch (err) {
    console.error('파이프라인 실행 중 치명적 에러 발생:', err);
  }
}

// 스크립트 실행
processAndIngest();
