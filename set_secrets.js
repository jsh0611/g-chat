// Supabase Edge Function 시크릿 일괄 등록 스크립트
// .env의 값(OPENAI_API_KEY, base_url, OPENAI_API_KEY_2, TOUR_API_KEY)을 읽어
// SUPABASE_URL에서 자동 추출한 project ref에 등록한다. (SUPABASE_* 3종은 자동 주입이라 제외)
// 실행: node set_secrets.js
import { spawnSync } from "child_process";
import fs from "fs";
import os from "os";
import path from "path";
import dotenv from "dotenv";

dotenv.config();

const url = process.env.SUPABASE_URL ?? "";
const ref = url.match(/https:\/\/([a-z0-9]+)\.supabase\.co/)?.[1];
if (!ref) {
  console.error(
    "❌ SUPABASE_URL에서 project ref를 찾지 못했습니다:",
    url || "(값 없음)",
  );
  process.exit(1);
}

const secrets = {
  OPENAI_API_KEY: process.env.OPENAI_API_KEY, // factchat 게이트웨이 (채팅)
  OPENAI_BASE_URL: process.env.OPENAI_BASE_URL ?? process.env.base_url,
  OPENAI_API_KEY_2: process.env.OPENAI_API_KEY_2, // 임베딩 전용 OpenAI 키
  TOUR_API_KEY: process.env.TOUR_API_KEY, // data.go.kr (TourAPI + TAGO 공용)
};
const missing = Object.entries(secrets)
  .filter(([, v]) => !v)
  .map(([k]) => k);
if (missing.length) {
  console.error("❌ .env에 값이 없습니다:", missing.join(", "));
  process.exit(1);
}

// 시크릿이 커맨드라인(프로세스 목록·셸 히스토리)에 노출되지 않도록 임시 env 파일로 전달
const tmp = path.join(os.tmpdir(), `supabase-secrets-${Date.now()}.env`);
fs.writeFileSync(
  tmp,
  Object.entries(secrets)
    .map(([k, v]) => `${k}=${v}`)
    .join("\n"),
);

console.log(
  `🔐 프로젝트 ${ref}에 시크릿 ${Object.keys(secrets).length}개 등록 중...`,
);
const r = spawnSync(
  "npx",
  ["supabase", "secrets", "set", "--env-file", tmp, "--project-ref", ref],
  {
    stdio: "inherit",
    shell: true, // Windows에서 npx 실행에 필요
  },
);
fs.unlinkSync(tmp);

if ((r.status ?? 1) !== 0) {
  console.error(
    "❌ 등록 실패 — supabase CLI 로그인 여부(npx supabase login)를 확인하세요.",
  );
  process.exit(r.status ?? 1);
}
console.log("✅ 완료. 확인: npx supabase secrets list --project-ref " + ref);
