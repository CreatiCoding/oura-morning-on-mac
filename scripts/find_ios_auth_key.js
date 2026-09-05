#!/usr/bin/env node
/*
 * iOS Oura 앱의 Realm에서 16바이트 auth_key를 탐색한다.
 * Android와 스키마(클래스/필드명)가 다를 수 있으므로, 스키마를 덤프하고
 * 16바이트 Data(ArrayBuffer) 프로퍼티를 가진 객체를 찾아 후보로 제시한다.
 *
 * 준비: 이 스크립트가 있는 폴더에서  npm install realm@20
 * 사용: node find_ios_auth_key.js <realm파일> [serial] [out-key-file]
 *   - serial 주면 해당 시리얼 매칭 행 우선. out-key-file 주면 확정 시 0600으로 기록.
 *   - 키 자체는 stdout에 절대 출력하지 않음(후보 위치/메타만 출력).
 */
const fs = require("fs");
let Realm;
try { Realm = require("realm"); }
catch { Realm = require(require.resolve("realm", { paths: [process.cwd()] })); }

const [realmPath, serial, outPath] = process.argv.slice(2);
if (!realmPath) {
  console.error("usage: find_ios_auth_key.js <realm> [serial] [out-key-file]");
  process.exit(2);
}

const realm = new Realm.Realm({ path: realmPath, readOnly: true });
try {
  const schema = realm.schema;
  console.log(`[i] Realm 스키마: ${schema.length} 클래스`);

  // 16바이트 data 프로퍼티를 가진 클래스 탐색
  const candidates = [];
  for (const cls of schema) {
    const dataProps = Object.entries(cls.properties)
      .filter(([, p]) => (p.type === "data"))
      .map(([name]) => name);
    if (dataProps.length === 0) continue;

    let rows;
    try { rows = Array.from(realm.objects(cls.name)); } catch { continue; }

    for (const row of rows) {
      for (const prop of dataProps) {
        const val = row[prop];
        if (val && val.byteLength === 16) {
          // 시리얼 비슷한 문자열 프로퍼티 수집
          const strFields = {};
          for (const [pn, p] of Object.entries(cls.properties)) {
            if (p.type === "string" && row[pn]) strFields[pn] = row[pn];
          }
          candidates.push({ cls: cls.name, prop, strFields });
        }
      }
    }
  }

  if (candidates.length === 0) {
    console.log("[!] 16바이트 data 프로퍼티를 가진 행이 없음. 전체 스키마 덤프:");
    for (const cls of schema) {
      const props = Object.entries(cls.properties)
        .map(([n, p]) => `${n}:${p.type}`).join(", ");
      console.log(`  ${cls.name} { ${props} }`);
    }
    process.exit(1);
  }

  console.log(`[i] 16바이트 키 후보 ${candidates.length}개:`);
  candidates.forEach((c, i) => {
    console.log(`  [${i}] class=${c.cls} field=${c.prop} strings=${JSON.stringify(c.strFields)}`);
  });

  // 확정 로직: serial 매칭되는 후보 우선
  let chosen = null;
  if (serial) {
    chosen = candidates.find(c =>
      Object.values(c.strFields).some(v => String(v).includes(serial)));
  }
  if (!chosen && candidates.length === 1) chosen = candidates[0];

  if (chosen && outPath) {
    // 재조회해서 실제 바이트 기록
    const rows = Array.from(realm.objects(chosen.cls));
    const row = serial
      ? rows.find(r => Object.entries(r).some(([, v]) => String(v).includes(serial)))
      : rows.find(r => r[chosen.prop] && r[chosen.prop].byteLength === 16);
    const hex = Buffer.from(row[chosen.prop]).toString("hex");
    fs.writeFileSync(outPath, `${hex}\n`, { mode: 0o600 });
    fs.chmodSync(outPath, 0o600);
    console.log(`[✓] auth_key(16B)를 ${outPath} 에 기록 (class=${chosen.cls} field=${chosen.prop})`);
  } else if (chosen) {
    console.log(`[i] 확정 후보: class=${chosen.cls} field=${chosen.prop} — out-key-file 인자를 주면 기록함`);
  } else {
    console.log("[!] 자동 확정 실패(후보 여러 개). 위 목록에서 시리얼/맥주소로 맞는 것 선택 필요.");
  }
} finally {
  realm.close();
}
