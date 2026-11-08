import { createClient } from '@supabase/supabase-js';
import dotenv from 'dotenv';

dotenv.config();

// RLS 우회를 위해 SERVICE_ROLE_KEY 우선 사용, 없으면 ANON_KEY 사용
const supabase = createClient(
  process.env.SUPABASE_URL,
  process.env.SUPABASE_SERVICE_ROLE_KEY || process.env.SUPABASE_ANON_KEY
);

// 미리보기로 출력할 문서 개수
const PREVIEW_COUNT = 5;
// content 미리보기 최대 글자 수
const PREVIEW_LEN = 120;

/**
 * documents 테이블에 텍스트가 잘 올라갔는지 확인하는 스크립트
 */
async function checkDocuments() {
  console.log('🔍 documents 테이블 확인을 시작합니다...\n');

  try {
    // 1. 전체 행 개수 확인
    const { count, error: countError } = await supabase
      .from('documents')
      .select('*', { count: 'exact', head: true });

    if (countError) {
      console.error('❌ 개수 조회 실패:', countError.message);
      return;
    }

    console.log(`📊 총 저장된 문서(청크) 수: ${count}개`);

    if (!count || count === 0) {
      console.log('\n⚠️  DB에 저장된 문서가 없습니다. 적재(ingest)가 정상적으로 되었는지 확인하세요.');
      return;
    }

    // 2. 최근 저장된 문서 일부 미리보기
    const { data: docs, error: dataError } = await supabase
      .from('documents')
      .select('id, content, metadata')
      .order('id', { ascending: false })
      .limit(PREVIEW_COUNT);

    if (dataError) {
      console.error('❌ 데이터 조회 실패:', dataError.message);
      return;
    }

    console.log(`\n=== 최근 ${docs.length}개 문서 미리보기 ===\n`);
    docs.forEach((doc, i) => {
      const content = doc.content || '';
      const preview =
        content.length > PREVIEW_LEN
          ? content.slice(0, PREVIEW_LEN) + '...'
          : content;

      console.log(`[${i + 1}] id: ${doc.id}`);
      console.log(`    내용: ${preview}`);
      console.log(`    글자 수: ${content.length}`);
      console.log(`    메타데이터: ${JSON.stringify(doc.metadata)}`);
      console.log('');
    });

    // 3. 임베딩(벡터)이 정상적으로 저장되었는지 확인
    const { data: sample, error: embedError } = await supabase
      .from('documents')
      .select('id, embedding')
      .limit(1)
      .single();

    if (embedError) {
      console.error('⚠️  임베딩 확인 실패:', embedError.message);
    } else if (sample && sample.embedding) {
      // embedding은 문자열 또는 배열로 반환될 수 있으므로 둘 다 처리
      const embedding =
        typeof sample.embedding === 'string'
          ? JSON.parse(sample.embedding)
          : sample.embedding;
      console.log('=== 임베딩(벡터) 확인 ===');
      console.log(`샘플 id: ${sample.id}`);
      console.log(`벡터 차원 수: ${embedding.length} (정상값: 1536)`);
      console.log(`상태: ${embedding.length === 1536 ? '✅ 정상' : '⚠️ 차원 불일치'}`);
    } else {
      console.log('⚠️  임베딩이 비어 있습니다.');
    }

    // 4. 출처별 임베딩 NULL 행 수 — 위 3번은 행 1개만 보므로(그 행이 채워져 있으면 '정상') 전체 NULL 을
    //    놓친다. 실제로 2026-08 에 NULL 5천여 행이 이 검사를 통과한 채 방치됐다.
    //    NULL 이 하나라도 있으면 종료 코드 3 → refresh_data.ps1 이 실패로 끝난다
    //    (-AllowNullEmbeddings 면 경고로만). 그 밖의 오류는 종료 코드 1.
    console.log('\n=== 출처별 임베딩 NULL 행 ===');
    let nullTotal = 0;
    const sources = new Set();
    for (let from = 0; from < count; from += 1000) {
      const { data, error } = await supabase.from('documents').select('metadata->source')
        .order('id', { ascending: true }).range(from, from + 999);
      if (error) throw error;
      for (const r of data) sources.add(r.source ?? null);
    }
    for (const src of sources) {
      const base = () => supabase.from('documents').select('id', { count: 'exact', head: true });
      const bySrc = (q) => (src === null ? q.is('metadata->>source', null) : q.eq('metadata->>source', src));
      const { count: all, error: e1 } = await bySrc(base());
      const { count: nul, error: e2 } = await bySrc(base()).is('embedding', null);
      if (e1 || e2) throw e1 || e2;
      nullTotal += nul;
      console.log(`${nul ? '❌' : '✅'} ${src ?? '(source 없음)'}: NULL ${nul} / ${all}`);
    }
    if (nullTotal > 0) {
      console.error(`\n❌ 임베딩 NULL ${nullTotal}행 — node embed_backfill.js 로 채우세요 (벡터 검색에서 빠져 있음).`);
      process.exitCode = 3;
      return;
    }

    console.log('\n🎉 확인 완료!');
  } catch (err) {
    console.error('확인 중 치명적 에러 발생:', err);
    process.exitCode = 1;
  }
}

checkDocuments();
