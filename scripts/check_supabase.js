import { createClient } from '@supabase/supabase-js';
import dotenv from 'dotenv';

dotenv.config();

const { SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY } = process.env;

/**
 * Supabase 프로젝트가 정상적으로 응답하는지 확인하는 헬스체크 스크립트
 * (환경변수 -> 연결 -> 인증 키 -> 테이블 접근 순으로 확인)
 */
async function checkSupabase() {
  console.log('🔍 Supabase 상태 확인을 시작합니다...\n');

  // 1. 환경변수 확인
  console.log('=== 1. 환경변수 확인 ===');
  if (!SUPABASE_URL) {
    console.error('❌ SUPABASE_URL이 설정되어 있지 않습니다. .env 파일을 확인하세요.');
    return;
  }
  console.log(`✅ SUPABASE_URL: ${SUPABASE_URL}`);
  console.log(`${SUPABASE_ANON_KEY ? '✅' : '⚠️ '} SUPABASE_ANON_KEY: ${SUPABASE_ANON_KEY ? '설정됨' : '없음'}`);
  console.log(`${SUPABASE_SERVICE_ROLE_KEY ? '✅' : '⚠️ '} SUPABASE_SERVICE_ROLE_KEY: ${SUPABASE_SERVICE_ROLE_KEY ? '설정됨' : '없음'}`);

  if (!SUPABASE_ANON_KEY && !SUPABASE_SERVICE_ROLE_KEY) {
    console.error('\n❌ 사용할 수 있는 키가 없습니다 (ANON_KEY 또는 SERVICE_ROLE_KEY 필요).');
    return;
  }

  // 2. REST 엔드포인트 자체가 살아있는지 확인 (인증 키 없이 순수 네트워크 체크)
  console.log('\n=== 2. 네트워크 연결 확인 ===');
  try {
    const start = Date.now();
    const res = await fetch(`${SUPABASE_URL}/rest/v1/`, {
      headers: { apikey: SUPABASE_ANON_KEY || SUPABASE_SERVICE_ROLE_KEY },
    });
    const elapsed = Date.now() - start;
    console.log(`✅ REST 엔드포인트 응답: HTTP ${res.status} (${elapsed}ms)`);
  } catch (err) {
    console.error('❌ Supabase 서버에 연결할 수 없습니다:', err.message);
    console.error('   URL이 올바른지, 인터넷 연결 상태를 확인하세요.');
    return;
  }

  // 3. 키별로 실제 쿼리가 통과하는지 확인
  console.log('\n=== 3. 키(권한)별 쿼리 확인 ===');
  const keysToTest = [
    { name: 'ANON_KEY', key: SUPABASE_ANON_KEY },
    { name: 'SERVICE_ROLE_KEY', key: SUPABASE_SERVICE_ROLE_KEY },
  ].filter((k) => k.key);

  let anyOk = false;
  for (const { name, key } of keysToTest) {
    const client = createClient(SUPABASE_URL, key);
    try {
      const { count, error } = await client
        .from('documents')
        .select('*', { count: 'exact', head: true });

      if (error) {
        console.log(`❌ ${name}: 쿼리 실패 - ${error.message}`);
      } else {
        console.log(`✅ ${name}: 정상 (documents 테이블 행 수: ${count})`);
        anyOk = true;
      }
    } catch (err) {
      console.log(`❌ ${name}: 예외 발생 - ${err.message}`);
    }
  }

  console.log('\n' + (anyOk ? '🎉 Supabase가 정상적으로 동작하고 있습니다.' : '⚠️  모든 키에서 쿼리가 실패했습니다. 키 값과 테이블/RLS 설정을 확인하세요.'));
}

checkSupabase();
