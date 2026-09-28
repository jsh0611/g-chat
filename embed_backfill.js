// 임베딩 백필: documents 테이블에서 embedding이 NULL인 행을 골라 Gemini로 채운다.
//
// 임베딩: embed_utils.js (gemini-embedding-001, 1536차원, GEMINI_API_KEY)
// 무료 티어 분당 한도(요청 수·토큰 수) 때문에 배치(50개)마다 1분씩 쉰다 — 5,758청크 기준 약 2시간.
// 한도 소진 등으로 중단돼도 멱등이라 아무 때나 다시 실행하면 이어서 채운다.
//
// 실행: node embed_backfill.js
// 임베딩 모델을 바꿨을 때(예: OpenAI → Gemini): REEMBED_ALL=true node embed_backfill.js
//   → 기존 벡터를 전부 NULL로 비운 뒤 새로 채운다. 모델이 다른 벡터끼리는 비교가 안 되므로
//     섞여 있으면 안 된다. (비운 동안은 봇이 키워드 검색 + 채워진 청크만으로 동작)
import { createClient } from '@supabase/supabase-js';
import dotenv from 'dotenv';
import { embedBatch, pace, EmbedQuotaError, EMBED_BATCH_SIZE } from './embed_utils.js';

dotenv.config({ quiet: true });

const supabase = createClient(
  process.env.SUPABASE_URL,
  process.env.SUPABASE_SERVICE_ROLE_KEY
);

async function main() {
  if (process.env.REEMBED_ALL === 'true') {
    const { error } = await supabase
      .from('documents')
      .update({ embedding: null })
      .not('embedding', 'is', null);
    if (error) throw error;
    console.log('🧹 기존 임베딩을 모두 비웠습니다 (모델 교체).');
  }

  const { count } = await supabase
    .from('documents')
    .select('id', { count: 'exact', head: true })
    .is('embedding', null);
  console.log(`🎯 임베딩 없는 청크: ${count}개 (예상 소요 약 ${Math.ceil(count / EMBED_BATCH_SIZE)}분)`);

  let filled = 0;
  while (true) {
    const { data: rows, error } = await supabase
      .from('documents')
      .select('id, content')
      .is('embedding', null)
      .order('id', { ascending: true })
      .limit(EMBED_BATCH_SIZE);
    if (error) throw error;
    if (!rows || rows.length === 0) {
      console.log('\n🎉 모든 청크에 임베딩이 채워졌습니다!');
      return;
    }

    let embeddings;
    try {
      embeddings = await embedBatch(rows.map((r) => r.content));
    } catch (err) {
      console.log(`\n⏸️ 중단(${(err.message ?? '').slice(0, 120)}) — 이번 실행에서 ${filled}개 채움, ${count - filled}개 남음.`);
      console.log(`   ${err instanceof EmbedQuotaError ? '내일 ' : ''}다시 실행하면 이어서 채웁니다: node embed_backfill.js`);
      return;
    }

    // 행별 업데이트 (id 기준)
    for (let i = 0; i < rows.length; i++) {
      const { error: upErr } = await supabase
        .from('documents')
        .update({ embedding: embeddings[i] })
        .eq('id', rows[i].id);
      if (upErr) console.error(`❌ id=${rows[i].id} 업데이트 실패:`, upErr.message);
    }
    filled += rows.length;
    console.log(`✅ 채움: ${filled}/${count}`);
    await pace();
  }
}

main().catch((e) => {
  console.error('백필 중단:', e.message);
  process.exit(1);
});
