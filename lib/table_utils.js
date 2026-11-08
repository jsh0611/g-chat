// 크롤링된 마크다운 표 전처리 유틸.
//
// gpt-4.1-nano가 마크다운 표(특히 병합 셀이 풀려 행이 쪼개진 표)를 잘 읽지 못해
// 표를 "레코드 문장" 형태의 줄 목록으로 변환한다. (test_table_diag.js로 검증: 0/4 → 3~4/4)
//
// 변환 규칙:
//  1. 무의미한 자동 헤더(열1, 열2...)는 제거, 실제 헤더는 필드 라벨로 사용
//  2. 첫 셀 라벨이 같은 연속 행(rowspan 풀림)은 한 행으로 병합 (값은 " / "로 연결)
//  3. 첫 데이터 행이 "구 분"으로 시작하는 열 방향 표는 전치하여
//     엔티티(반/시설)별 한 줄 레코드로 출력
//  4. 그 외 표는 행별 레코드로 출력 (2열이면 "왼쪽: 오른쪽", 그 이상이면 " | " 연결)

function splitRow(line) {
  return line
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((c) => c.trim());
}

function isSeparatorRow(line) {
  return /^\|[\s\-:|]+\|$/.test(line.trim());
}

function isTableRow(line) {
  const t = line.trim();
  return t.startsWith('|') && t.endsWith('|') && t.length > 1;
}

function isGenericHeader(cells) {
  return cells.every((c) => /^열\s*\d+$/.test(c) || c === '');
}

const isGubun = (s) => /^구\s*분$/.test(s);

/** 첫 셀이 같은 연속 행 병합 (rowspan 풀림 보정) */
function mergeDuplicateLabelRows(rows) {
  const out = [];
  for (const cells of rows) {
    const prev = out[out.length - 1];
    if (prev && prev.length === cells.length && prev[0] === cells[0] && cells[0] !== '') {
      for (let i = 1; i < cells.length; i++) {
        if (cells[i] && !prev[i].split(', ').includes(cells[i])) {
          prev[i] = prev[i] ? `${prev[i]}, ${cells[i]}` : cells[i];
        }
      }
    } else {
      out.push([...cells]);
    }
  }
  return out;
}

/** 표 하나(줄 배열)를 레코드 문장 줄 배열로 변환 */
function tableToRecords(tableLines) {
  let rows = tableLines.filter((l) => !isSeparatorRow(l)).map(splitRow);
  if (rows.length === 0) return [];

  // 헤더 분리
  let header = null;
  if (isGenericHeader(rows[0])) {
    rows = rows.slice(1); // 자동 생성 헤더(열1...)는 버림
  } else if (rows.length > 1) {
    header = rows[0];
    rows = rows.slice(1);
    // 2단 헤더 병합: 첫 데이터 행의 첫 셀이 헤더 첫 셀과 같으면 하위 헤더로 간주
    // 예: 헤더 "사용료|사용료" + 하위 "1개월 등록|3개월 등록" → "사용료(1개월 등록)|사용료(3개월 등록)"
    if (rows.length > 1 && rows[0].length === header.length && rows[0][0] === header[0]) {
      header = header.map((h, i) =>
        rows[0][i] && rows[0][i] !== h ? `${h}(${rows[0][i]})` : h
      );
      rows = rows.slice(1);
    }
  }

  // 헤더에 URL이 들어 있으면 데이터 행을 헤더로 오인한 것 (신청링크 표 등은 헤더가 없음).
  // 헤더 오인 시 "기초반1 링크: 중급반 링크"처럼 무관한 값끼리 라벨-값으로 묶여 LLM이 오독함.
  if (header && header.some((c) => /https?:\/\//.test(c))) {
    rows.unshift(header);
    header = null;
  }

  // rowspan 풀림 병합은 크롤러 자동 헤더(열N) 표에서만 발생하는 병리이므로 그 경우로 한정.
  // 실제 헤더가 있는 정상 표에서 첫 셀 반복은 하위 행(예: 요가 주간/야간)이라 합치면 매핑이 깨짐.
  if (!header) {
    rows = mergeDuplicateLabelRows(rows);
  }
  if (rows.length === 0) return [];

  // 공통 열 추출: 3행 이상에서 모든 행의 값이 동일한 열(예: 매 행 반복되는 "비고")은
  // 행에서 제거하고 각주 한 줄로 뺀다 — 행이 짧아져 청크에 표 전체가 들어가고 임베딩 노이즈도 줄어듦
  const footnotes = [];
  if (rows.length >= 3) {
    const width0 = Math.max(...rows.map((r) => r.length));
    for (let j = width0 - 1; j >= 1; j--) {
      const values = rows.map((r) => r[j] ?? '');
      const first = values[0];
      if (first && values.every((v) => v === first)) {
        footnotes.unshift(`※ ${header?.[j] && header[j] !== first ? header[j] + ': ' : ''}${first}`);
        rows = rows.map((r) => r.filter((_, i) => i !== j));
        if (header) header = header.filter((_, i) => i !== j);
      }
    }
  }

  const width = Math.max(...rows.map((r) => r.length));

  // 1열 표: 본문 문단으로 그대로 출력
  if (width === 1) {
    return [...rows.map((r) => r[0]).filter(Boolean), ...footnotes];
  }

  // 열 방향 표(구 분 + 속성 행들): 전치하여 엔티티별 레코드 생성
  if (isGubun(rows[0][0]) && rows.length >= 2) {
    const entityRow = rows[0];
    const attrRows = rows.slice(1);
    const records = [];
    for (let j = 1; j < entityRow.length; j++) {
      const entity = entityRow[j];
      if (!entity) continue;
      const pairs = attrRows
        .filter((r) => r[0] && r[j])
        .map((r) => `${r[0]}: ${r[j]}`);
      records.push(pairs.length ? `- ${entity} — ${pairs.join(' | ')}` : `- ${entity}`);
    }
    if (records.length > 0) return [...records, ...footnotes];
  }

  // 행 방향 표: 행별 레코드
  const records = rows.map((cells) => {
    const filled = cells.filter(Boolean);
    if (filled.length === 0) return null;
    if (header) {
      // 같은 헤더 라벨이 연속되면 값을 합침 (예: 프로그램명|프로그램명 → "프로그램명: 요가 주간")
      const pairs = [];
      for (let i = 0; i < cells.length; i++) {
        if (!cells[i]) continue;
        const label = header[i] ?? '';
        const prev = pairs[pairs.length - 1];
        if (prev && label && prev.label === label) {
          prev.value += ` ${cells[i]}`;
        } else {
          pairs.push({ label, value: cells[i] });
        }
      }
      return `- ${pairs.map((p) => (p.label ? `${p.label}: ${p.value}` : p.value)).join(' | ')}`;
    }
    if (filled.length === 2) return `- ${filled[0]}: ${filled[1]}`;
    return `- ${filled.join(' | ')}`;
  }).filter(Boolean);
  return [...records, ...footnotes];
}

/** 문서 전체 텍스트에서 마크다운 표 블록을 찾아 레코드 문장으로 변환 */
export function restructureTables(content) {
  const lines = content.split('\n');
  const out = [];
  let tableBuf = [];

  const flushTable = () => {
    if (tableBuf.length > 0) {
      out.push(...tableToRecords(tableBuf));
      tableBuf = [];
    }
  };

  for (const line of lines) {
    if (isTableRow(line)) {
      tableBuf.push(line);
    } else {
      flushTable();
      out.push(line);
    }
  }
  flushTable();
  return out.join('\n');
}
