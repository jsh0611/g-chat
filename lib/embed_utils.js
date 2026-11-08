// 임베딩 공용 모듈 (ingest.js · ingest_tour.js · embed_backfill.js · check_gateway.js 공용)
//
// 모델: Google Gemini gemini-embedding-001 (GEMINI_API_KEY)
//   - LLM 게이트웨이(factchat)에는 임베딩 API가 없어 별도 키를 쓴다.
//   - outputDimensionality=1536으로 기존 vector(1536) 스키마를 그대로 쓴다.
//   - 3072 미만 차원은 정규화돼 있지 않아 직접 L2 정규화한다.
//   - 문서는 RETRIEVAL_DOCUMENT, 질문은 RETRIEVAL_QUERY로 임베딩한다 (Edge Function은 QUERY).
// ⚠️ 무료 티어는 분당 요청 수(배치 안의 항목 수 기준 ~100건)와 분당 토큰 수 한도가 함께 걸린다.
//    한국어 500자 청크 100개 ≈ 4.2만 토큰이라 배치 100개는 영원히 429 → 배치 50개(≈2.1만 토큰)마다 1분씩 쉰다.
//    429가 오면 retryDelay(없으면 60초)만큼 기다렸다 재시도하고, 일일 한도 소진이면 EmbedQuotaError로 중단한다.
//
// 하루 사용량 예산: 무료 티어 일일 한도는 임베딩 1,000건(배치 안 항목 하나 = 1건, 2026-09 실측 —
//   EmbedContentRequestsPerDayPerUserPerProjectPerModel-FreeTier). 봇(Edge Function)의 질문 임베딩도 같은
//   한도를 쓰므로, 로컬 스크립트는 하루 EMBED_RESERVE(기본 200)건을 봇 몫으로 남기고 멈춘다.
//   사용량은 logs/embed_usage.json 에 태평양 시간 날짜(한도가 리셋되는 기준)별로 누적한다.
//   봇이 쓴 양은 여기 기록되지 않으므로 예약분이 그 몫이다.
//   조정: EMBED_DAILY_LIMIT(기본 1000) · EMBED_RESERVE(기본 200)
import dotenv from 'dotenv';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

dotenv.config({ quiet: true });

export const EMBED_MODEL = 'gemini-embedding-001';
export const EMBED_DIM = 1536;
export const EMBED_BATCH_SIZE = 50;
const ENDPOINT = `https://generativelanguage.googleapis.com/v1beta/models/${EMBED_MODEL}:batchEmbedContents`;

export class EmbedQuotaError extends Error {}
/** 로컬 예산(봇 몫 예약) 소진 — 일일 한도 소진과 같은 취급(내일 이어서) */
export class EmbedBudgetError extends EmbedQuotaError {}

const DAILY_LIMIT = parseInt(process.env.EMBED_DAILY_LIMIT ?? '1000', 10);
// 테스트·챗봇용으로 기본 200건을 남기고 로컬 적재는 하루 최대 800건 사용한다.
const RESERVE = parseInt(process.env.EMBED_RESERVE ?? '200', 10);
const USAGE_FILE = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'logs', 'embed_usage.json');

/** 한도 리셋 기준 날짜 (태평양 시간 YYYY-MM-DD) */
export const pacificDate = (d = new Date()) =>
  new Intl.DateTimeFormat('en-CA', { timeZone: 'America/Los_Angeles' }).format(d);

function readUsage() {
  try { return JSON.parse(fs.readFileSync(USAGE_FILE, 'utf-8')); } catch { return {}; }
}

/** 오늘(태평양 시간) 로컬 스크립트가 쓴 임베딩 건수 */
export function usedToday() {
  return readUsage()[pacificDate()]?.total ?? 0;
}

/** 오늘 남은 로컬 예산 = 일일 한도 − 봇 몫 − 오늘 사용량 */
export function budgetLeft() {
  return Math.max(0, DAILY_LIMIT - RESERVE - usedToday());
}

function recordUsage(n) {
  const usage = readUsage();
  const day = pacificDate();
  const script = path.basename(process.argv[1] ?? '?');
  usage[day] ??= { total: 0, by_script: {} };
  usage[day].total += n;
  usage[day].by_script[script] = (usage[day].by_script[script] ?? 0) + n;
  fs.mkdirSync(path.dirname(USAGE_FILE), { recursive: true });
  fs.writeFileSync(USAGE_FILE, JSON.stringify(usage, null, 2));
}

const normalize = (v) => {
  const n = Math.hypot(...v) || 1;
  return v.map((x) => x / n);
};

/**
 * 텍스트 배열 → 정규화된 1536차원 벡터 배열 (최대 EMBED_BATCH_SIZE개).
 * taskType: 'RETRIEVAL_DOCUMENT'(적재용, 기본) | 'RETRIEVAL_QUERY'(검색 질문용)
 */
export async function embedBatch(inputs, taskType = 'RETRIEVAL_DOCUMENT') {
  const key = process.env.GEMINI_API_KEY;
  if (!key) throw new Error('.env에 GEMINI_API_KEY가 없습니다.');
  // 문서 적재용 임베딩만 예산을 따진다 (질문용 RETRIEVAL_QUERY 는 테스트 등 소량)
  if (taskType === 'RETRIEVAL_DOCUMENT' && inputs.length > budgetLeft()) {
    throw new EmbedBudgetError(
      `오늘(태평양 ${pacificDate()}) 로컬 임베딩 예산 소진 — 사용 ${usedToday()} / 한도 ${DAILY_LIMIT} − 봇 몫 ${RESERVE}. ` +
      '한도 리셋(한국 시간 오후 4~5시) 후 다시 실행하면 이어서 채웁니다.');
  }
  for (let attempt = 1; ; attempt++) {
    const res = await fetch(ENDPOINT, {
      method: 'POST',
      headers: { 'x-goog-api-key': key, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        requests: inputs.map((text) => ({
          model: `models/${EMBED_MODEL}`,
          content: { parts: [{ text }] },
          taskType,
          outputDimensionality: EMBED_DIM,
        })),
      }),
    });
    if (res.ok) {
      const { embeddings } = await res.json();
      recordUsage(inputs.length);
      return embeddings.map((e) => normalize(e.values));
    }
    const body = await res.text();
    if (res.status === 429 && /PerDay/i.test(body)) {
      // 한도 이름·값을 함께 남긴다 (무료 티어 일일 한도는 문서에 없고 계정별로 달라 실측이 유일한 근거)
      const quotaId = body.match(/"quotaId":\s*"([^"]+)/)?.[1] ?? '?';
      const quotaValue = body.match(/"quotaValue":\s*"?(\d+)/)?.[1] ?? '?';
      throw new EmbedQuotaError(`Gemini 임베딩 일일 한도 소진 (${quotaId} = ${quotaValue}) — 내일 다시 실행하면 이어서 채웁니다.`);
    }
    if (attempt >= 6) throw new Error(`임베딩 실패 HTTP ${res.status}: ${body.slice(0, 200)}`);
    // 429면 서버가 알려준 retryDelay(예: "37s")만큼, 그 외 일시 오류는 점증 대기
    const retry = Number(body.match(/"retryDelay":\s*"(\d+)/)?.[1]);
    const wait = res.status === 429 ? ((retry || 60) + 2) * 1000 : attempt * 10000;
    const quota = body.match(/"quotaId":\s*"([^"]+)/)?.[1] ?? '';
    console.warn(`⚠️ 임베딩 HTTP ${res.status} ${quota} — ${Math.round(wait / 1000)}초 후 재시도 (${attempt}/6)`);
    await new Promise((r) => setTimeout(r, wait));
  }
}

/** 분당 한도를 넘지 않도록 배치 사이에 쉬는 시간 (배치 50개 기준 1분) */
export const pace = () => new Promise((r) => setTimeout(r, 61000));
