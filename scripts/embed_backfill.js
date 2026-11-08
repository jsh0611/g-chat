// 임베딩 백필: documents 테이블에서 embedding이 NULL인 행을 골라 Gemini로 채운다.
//
// 임베딩: embed_utils.js (gemini-embedding-001, 1536차원, GEMINI_API_KEY)
// 무료 티어 분당 한도(요청 수·토큰 수) 때문에 배치(50개)마다 1분씩 쉰다.
// 한도 소진 등으로 중단돼도 멱등이라 아무 때나 다시 실행하면 남은 NULL 부터 이어서 채운다.
//
// 채우는 순서(우선순위) — 일일 한도 안에 다 못 채우면 봇에 영향이 큰 것부터:
//   1) 관광 POI(tourapi)
//   2) 공단 상시 정보(facility·page)와 올해(2026) 공지
//   3) 나머지 전부
// 임베딩 텍스트 = 저장 본문에서 임베딩 뒤에 덧붙인 "…예약 홈페이지: URL" 줄을 뺀 것(= 적재 때 임베딩 입력).
//
// 종료 코드: 0 = 전부 채움 / 2 = 일일 한도 소진(내일 이어서) / 1 = 그 밖의 실패
//   (예전엔 한도 소진·실패에도 0으로 끝나 자동 실행에서 멈춘 걸 알 수 없었다 — 2026-08 NULL 5천여 행 방치 원인)
//
// 실행: node scripts/embed_backfill.js
// 임베딩 모델을 바꿨을 때(예: OpenAI → Gemini): REEMBED_ALL=true node scripts/embed_backfill.js
//   → 기존 벡터를 전부 NULL로 비운 뒤 새로 채운다. 모델이 다른 벡터끼리는 비교가 안 되므로
//     섞여 있으면 안 된다. (비운 동안은 봇이 키워드 검색 + 채워진 청크만으로 동작 —
//     일일 한도 안에 다 못 채우면 며칠간 벡터 검색이 대부분 꺼지므로, 끝날 때까지 매일 다시 실행할 것)
import { createClient } from '@supabase/supabase-js';
import dotenv from 'dotenv';
import {
  embedBatch, pace, EmbedQuotaError, EMBED_BATCH_SIZE, budgetLeft, usedToday, pacificDate,
} from '../lib/embed_utils.js';
import { stripInjected } from '../lib/ingest_utils.js';

dotenv.config({ quiet: true });

const supabase = createClient(
  process.env.SUPABASE_URL,
  process.env.SUPABASE_SERVICE_ROLE_KEY
);

const THIS_YEAR = String(new Date().getFullYear());
const TIERS = [
  { name: '1) 관광 POI', filter: (q) => q.eq('metadata->>source', 'tourapi') },
  { name: `2) 공단 상시 정보·${THIS_YEAR}년 공지`, filter: (q) => q.eq('metadata->>source', 'documents.jsonl')
      .or(`metadata->>doc_type.in.(facility,page),metadata->>date.gte.${THIS_YEAR}-01-01`) },
  { name: '3) 나머지', filter: (q) => q },
];

const countNull = async (filter = (q) => q) => (await filter(
  supabase.from('documents').select('id', { count: 'exact', head: true }).is('embedding', null))).count;

async function main() {
  if (process.env.REEMBED_ALL === 'true') {
    const { error } = await supabase
      .from('documents')
      .update({ embedding: null })
      .not('embedding', 'is', null);
    if (error) throw error;
    console.log('🧹 기존 임베딩을 모두 비웠습니다 (모델 교체).');
  }

  const total = await countNull();
  console.log(`🎯 임베딩 없는 청크: ${total}개 (예상 소요 약 ${Math.ceil(total / EMBED_BATCH_SIZE)}분)`);
  let earlier = 0;
  for (const t of TIERS) {   // 3순위('나머지') 필터는 전체라, 앞 순위를 뺀 수로 표시
    const n = await countNull(t.filter);
    console.log(`   ${t.name}: ${n - (t === TIERS.at(-1) ? earlier : 0)}개`);
    earlier += n;
  }

  let filled = 0;
  console.log(`   오늘(태평양 ${pacificDate()}) 로컬 사용 ${usedToday()} · 남은 예산 ${budgetLeft()} (봇 몫 예약 제외)`);
  for (const tier of TIERS) {
    while (true) {
      // 남은 예산이 배치보다 작으면 그만큼만 (0 이면 embedBatch 가 EmbedBudgetError → 종료 코드 2)
      const size = Math.max(1, Math.min(EMBED_BATCH_SIZE, budgetLeft()));
      const { data: rows, error } = await tier.filter(supabase
        .from('documents')
        .select('id, content')
        .is('embedding', null))
        .order('id', { ascending: true })
        .limit(size);
      if (error) throw error;
      if (!rows || rows.length === 0) break;

      let embeddings;
      try {
        embeddings = await embedBatch(rows.map((r) => stripInjected(r.content)));
      } catch (err) {
        const left = await countNull();
        console.log(`\n⏸️ 중단 [${tier.name}] (${(err.message ?? '').slice(0, 160)})`);
        console.log(`   이번 실행에서 ${filled}개 채움, ${left}개 남음. 다시 실행하면 남은 NULL 부터 이어서 채웁니다.`);
        process.exit(err instanceof EmbedQuotaError ? 2 : 1);
      }

      // 행별 업데이트 (id 기준)
      for (let i = 0; i < rows.length; i++) {
        const { error: upErr } = await supabase
          .from('documents')
          .update({ embedding: embeddings[i] })
          .eq('id', rows[i].id);
        if (upErr) throw new Error(`id=${rows[i].id} 업데이트 실패: ${upErr.message}`);
      }
      filled += rows.length;
      console.log(`✅ [${tier.name}] 채움: ${filled}/${total}`);
      await pace();
    }
  }
  console.log('\n🎉 모든 청크에 임베딩이 채워졌습니다!');
}

main().catch((e) => {
  console.error('백필 중단:', e.message);
  process.exit(1);
});
