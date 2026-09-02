// 임베딩 백필: documents 테이블에서 embedding이 NULL인 행을 골라 OpenAI로 채운다.
//
// 임베딩은 전용 키 OPENAI_API_KEY_2 사용 (채팅용 factchat 게이트웨이와 별도).
// 모델: text-embedding-3-small (1536차원, 정규화된 벡터 반환 — 후처리 불필요)
// 크레딧 소진 등으로 중단돼도 멱등이라 아무 때나 다시 실행하면 이어서 채운다.
//
// 실행: node embed_backfill.js
import { createClient } from '@supabase/supabase-js';
import OpenAI from 'openai';
import dotenv from 'dotenv';

dotenv.config();

const supabase = createClient(
  process.env.SUPABASE_URL,
  process.env.SUPABASE_SERVICE_ROLE_KEY
);
const embedClient = new OpenAI({ apiKey: process.env.OPENAI_API_KEY_2 });

const BATCH_SIZE = 100;

async function embedBatch(inputs) {
  for (let attempt = 1; ; attempt++) {
    try {
      const res = await embedClient.embeddings.create({
        model: 'text-embedding-3-small',
        input: inputs,
      });
      return res.data.map((d) => d.embedding);
    } catch (err) {
      // 크레딧 소진은 재시도해도 소용없음 → 상위로 올려서 종료
      if ((err.message ?? '').includes('insufficient_quota') || err.code === 'credit_balance_exhausted') throw err;
      if (attempt >= 5) throw err;
      const wait = attempt * 10000;
      console.warn(`⚠️ 임베딩 실패(${(err.message ?? '').slice(0, 100)}) — ${wait / 1000}초 후 재시도 (${attempt}/5)`);
      await new Promise((r) => setTimeout(r, wait));
    }
  }
}

async function main() {
  const { count } = await supabase
    .from('documents')
    .select('id', { count: 'exact', head: true })
    .is('embedding', null);
  console.log(`🎯 임베딩 없는 청크: ${count}개`);

  let filled = 0;
  while (true) {
    const { data: rows, error } = await supabase
      .from('documents')
      .select('id, content')
      .is('embedding', null)
      .order('id', { ascending: true })
      .limit(BATCH_SIZE);
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
      console.log('   다시 실행하면 이어서 채웁니다: node embed_backfill.js');
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
  }
}

main().catch((e) => {
  console.error('백필 중단:', e.message);
  process.exit(1);
});
