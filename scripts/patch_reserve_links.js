// 시설(facility) 문서의 "예약 바로가기" 실제 목적지 URL 주입 스크립트.
//
// 크롤러가 시설소개 페이지의 "예약 바로가기" 버튼 href(외부 예약 사이트, 예: campingkorea.or.kr)를
// 유실해 본문에 "예약 바로가기" 텍스트만 남음 → 봇이 예약 안내에 시설소개 페이지 링크를 붙이는 문제.
// 원본 페이지를 다시 읽어 예약 관련 앵커의 href를 추출하고, 해당 문서의 예약 안내 청크 끝에
// "예약 홈페이지: <URL>" 줄을 덧붙인다. 멱등(이미 주입된 문서는 스킵).
//
// ⚠️ ingest.js로 재적재하면 주입이 사라지므로, 재적재 후 이 스크립트를 다시 실행할 것.
//    (청크 본문만 수정하고 임베딩은 그대로 둠 — URL 몇 글자는 검색 의미에 영향 없음)
// 실행: node scripts/patch_reserve_links.js
import { createClient } from '@supabase/supabase-js';
import dotenv from 'dotenv';

dotenv.config();

const supabase = createClient(
  process.env.SUPABASE_URL,
  process.env.SUPABASE_SERVICE_ROLE_KEY
);

/** 페이지 HTML에서 예약 관련 앵커(텍스트에 "예약"+"바로가기")의 href 목록 추출 */
function extractReserveLinks(html, baseUrl) {
  const links = [];
  const re = /<a[^>]+href="([^"#]+)"[^>]*>([^<]*예약[^<]*바로가기[^<]*)<\/a>/g;
  for (const m of html.matchAll(re)) {
    const label = m[2].trim();
    let href = m[1];
    if (!href.startsWith('http')) href = new URL(href, baseUrl).href; // 상대경로 보정
    href = href.replace(/\|/g, '%7C'); // 카카오톡 자동 링크 인식이 |에서 끊기는 것 방지
    if (!links.some((l) => l.href === href)) links.push({ label, href });
  }
  return links;
}

/** 앵커 라벨을 "…예약 홈페이지" 형태로 정규화한다.
 *   "예약 바로가기"        → "예약 홈페이지"
 *   "수학여행예약 바로가기" → "수학여행 예약 홈페이지"
 *  범위가 한정된 예약처(천곡황금박쥐동굴은 대관·단체 예약 폼)는 그 수식어를 반드시 남긴다.
 *  일반 입장 예약처럼 안내되면 방문객이 오해하기 때문. 반대로 봇 프롬프트가 "예약 홈페이지: URL"
 *  문자열을 기준으로 링크를 고르므로, 어떤 라벨이든 "예약 홈페이지"로 끝나야 한다.
 */
function reserveLabel(rawLabel) {
  const base = rawLabel.replace(/\s*바로가기\s*$/, '').trim();
  const qualifier = base.replace(/예약\s*$/, '').trim();   // 끝의 "예약"을 떼어 수식어만 남김
  return qualifier ? `${qualifier} 예약 홈페이지` : '예약 홈페이지';
}

async function main() {
  // 시설 문서 목록 (doc_id별 대표 URL)
  const { data: heads, error } = await supabase
    .from('documents')
    .select('metadata')
    .eq('metadata->>doc_type', 'facility')
    .eq('metadata->>chunk_index', '0');
  if (error) throw error;

  let patched = 0, skipped = 0, noLink = 0;
  for (const h of heads ?? []) {
    const { doc_id: docId, title, url, source } = h.metadata;
    // 예약시스템 문서는 ingest_camping.js 가 예약 링크를 이미 넣는다 — 여기서 그 사이트를
    // 요청 간격 없이 수십 번 다시 받을 이유가 없으므로 건너뛴다.
    if (source === 'campingkorea') continue;
    if (!url) { noLink++; continue; }

    let links = [];
    try {
      const res = await fetch(url, { redirect: 'follow' });
      links = extractReserveLinks(await res.text(), url);
    } catch (e) {
      console.warn(`⚠️ ${title}: 페이지 조회 실패 (${String(e.message).slice(0, 80)})`);
      continue;
    }
    if (links.length === 0) { console.log(`— ${title}: 예약 링크 없음`); noLink++; continue; }

    // 이 문서에서 예약 안내가 있는 청크를 찾아 URL 줄을 덧붙인다
    const { data: chunks } = await supabase
      .from('documents')
      .select('id, content')
      .eq('metadata->>doc_id', docId)
      .ilike('content', '%예약 홈페이지에서 진행%');
    if (!chunks || chunks.length === 0) { console.log(`— ${title}: 예약 안내 청크 없음`); continue; }

    for (const c of chunks) {
      // 이전 실행이 주입한 줄을 걷어내고 다시 주입 (재실행·링크 변경에 안전)
      const base = c.content.split('\n').filter((ln) => !/^\S.* 홈페이지: https?:\/\//.test(ln)).join('\n');
      const lines = links.map((l) => `${reserveLabel(l.label)}: ${l.href}`);
      const next = `${base}\n${lines.join('\n')}`;
      if (next === c.content) { skipped++; continue; } // 이미 최신 상태
      const { error: upErr } = await supabase
        .from('documents')
        .update({ content: next })
        .eq('id', c.id);
      if (upErr) console.error(`❌ ${title}: 업데이트 실패 — ${upErr.message}`);
      else { patched++; console.log(`✅ ${title}: ${lines.join(' | ')}`); }
    }
  }
  console.log(`\n완료 — 주입 ${patched}청크, 기주입 스킵 ${skipped}, 예약 링크 없음 ${noLink}건`);
}

main().catch((err) => { console.error('치명적 에러:', err); process.exit(1); });
