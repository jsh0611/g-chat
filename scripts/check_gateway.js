import OpenAI from 'openai';
import dotenv from 'dotenv';
import { embedBatch, EMBED_MODEL } from '../lib/embed_utils.js';

dotenv.config({ quiet: true });

const { OPENAI_API_KEY, GEMINI_API_KEY, base_url } = process.env;
const baseURL = process.env.OPENAI_BASE_URL || base_url;
// 테스트할 모델명을 인자로 받는다: node scripts/check_gateway.js <model>
const MODEL = process.argv[2] || 'gpt-5.5';

/**
 * factchat 게이트웨이(OPENAI_API_KEY + base_url) 조합이 로컬에서 정상 동작하는지 확인하는 스크립트
 * (kakao-bot Edge Function이 답변 생성에 쓰는 것과 동일한 조합)
 */
async function checkGateway() {
  console.log('🔍 OpenAI 호환 게이트웨이 확인을 시작합니다...\n');

  console.log('=== 환경변수 확인 ===');
  console.log(`${OPENAI_API_KEY ? '✅' : '❌'} OPENAI_API_KEY: ${OPENAI_API_KEY ? '설정됨' : '없음'}`);
  console.log(`${baseURL ? '✅' : '❌'} base_url: ${baseURL || '없음'}`);
  console.log(`${GEMINI_API_KEY ? '✅' : '⚠️ '} GEMINI_API_KEY (임베딩용): ${GEMINI_API_KEY ? '설정됨' : '없음'}`);

  if (!OPENAI_API_KEY || !baseURL) {
    console.error('\n❌ OPENAI_API_KEY 또는 base_url이 없습니다. .env를 확인하세요.');
    return;
  }

  // 1. 채팅 게이트웨이(OPENAI_API_KEY + base_url) 확인 — 실제 답변 생성에 쓰는 조합
  console.log(`\n=== 채팅 게이트웨이 확인 (model: ${MODEL}) ===`);
  const gateway = new OpenAI({ apiKey: OPENAI_API_KEY, baseURL });
  try {
    const start = Date.now();
    const res = await gateway.chat.completions.create({
      model: MODEL,
      temperature: 0,
      max_tokens: 10,
      messages: [
        { role: 'system', content: '한 단어로만 답하세요.' },
        { role: 'user', content: 'ping이라고만 답해줘' },
      ],
    });
    const elapsed = Date.now() - start;
    console.log(`✅ 정상 응답 (${elapsed}ms): "${res.choices[0].message.content}"`);
  } catch (err) {
    console.error(`❌ 호출 실패 (SDK): HTTP ${err.status ?? '?'} - ${err.message}`);

    // openai SDK는 이 게이트웨이의 에러 응답 본문을 못 읽어오는 경우가 있어(본문 없는 404),
    // 원인 파악을 위해 fetch로 동일 요청을 다시 보내 실제 응답 본문을 확인한다.
    try {
      const res = await fetch(`${baseURL}/chat/completions`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${OPENAI_API_KEY}`,
        },
        body: JSON.stringify({
          model: MODEL,
          max_tokens: 10,
          messages: [{ role: 'user', content: 'ping' }],
        }),
      });
      const body = await res.text();
      console.error(`   fetch 재확인: HTTP ${res.status} - ${body}`);
    } catch (fetchErr) {
      console.error('   fetch 재확인도 실패:', fetchErr.message);
    }
    console.error('\n   → API 키가 만료/무효이거나 base_url이 잘못됐을 가능성이 높습니다.');
  }

  // 2. 참고용: 임베딩 키(GEMINI_API_KEY)는 이 게이트웨이와 무관하므로 별도 확인
  if (GEMINI_API_KEY) {
    console.log(`\n=== (참고) 임베딩 전용 키 확인 (GEMINI_API_KEY, ${EMBED_MODEL}) ===`);
    try {
      const [vec] = await embedBatch(['ping'], 'RETRIEVAL_QUERY');
      console.log(`✅ 정상 (벡터 차원: ${vec.length})`);
    } catch (err) {
      console.error(`❌ 호출 실패: ${err.message}`);
    }
  }
}

checkGateway();
