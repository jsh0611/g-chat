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
import dotenv from 'dotenv';

dotenv.config({ quiet: true });

export const EMBED_MODEL = 'gemini-embedding-001';
export const EMBED_DIM = 1536;
export const EMBED_BATCH_SIZE = 50;
const ENDPOINT = `https://generativelanguage.googleapis.com/v1beta/models/${EMBED_MODEL}:batchEmbedContents`;

export class EmbedQuotaError extends Error {}

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
      return embeddings.map((e) => normalize(e.values));
    }
    const body = await res.text();
    if (res.status === 429 && /PerDay/i.test(body)) {
      throw new EmbedQuotaError('Gemini 임베딩 일일 한도 소진 — 내일 다시 실행하면 이어서 채웁니다.');
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
