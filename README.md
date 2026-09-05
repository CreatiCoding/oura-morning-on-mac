# WakeReady

Oura 링(Gen5) 데이터를 밤중에 직접 읽어, **"충분히/건강하게 잤을 때" 아이폰 알람을 울리는** 맥 상주형 기상 시스템.

공식 앱/클라우드는 실시간 기상 판정을 못 한다(데이터가 기상 후에야 올라옴). 그래서 [open_oura](https://github.com/Th0rgal/open_oura)로 **링과 BLE 직접 통신**해 밤중에 판정한다.

```
밤: 링(착용) ──BLE(읽기전용)──▶ 맥(open_oura)
                                  │ N분마다 sleep-analyze + sync
                                  │ 총수면 / REM·깊은수면(추정) 판정
                                  ▼ 충족 or 안전상한 시각
                            iMessage "WAKEREADY" ──▶ 아이폰 단축어 자동화
                                                      ▶ 볼륨100% + 노래/유튜브 🎵
```

## 특징
- **클라우드 미접촉·읽기 전용** (공식 앱 데이터/페어링 무손상)
- **알람은 아이폰에서** — 맥→iMessage→단축어 자동화로 노래/유튜브 재생 (앱 상주 불필요, 무료)
- **건강 수면 모드** — 총시간 + REM/깊은수면(원시신호 휴리스틱 추정) 충족 시 기상
- **3중 안전장치** — 목표충족 / 안전상한 시각 / 실패 폴백. "알람 안 울림"이 없도록 설계

## 빠른 시작

### 1. 설치 (맥)
```bash
./scripts/setup.sh          # Rust 확인 → open_oura 클론/빌드 → ./bin/oura 링크
cp .env.example .env        # 설정 채우기 (값은 .env, gitignore됨)
```

### 2. 링 인증 키
`.env`의 `OURA_AUTH_KEY`(또는 `key.hex`). iPhone 암호화 백업 → `assa.sqlite`의
`ringconfiguration.auth_key`에서 추출. 자세한 절차는 [SETUP.md](SETUP.md).

### 3. 아이폰 알람 자동화 (1회)
단축어 앱 → 자동화 → **메시지** → "메시지 포함 내용"=`WAKEREADY` → **즉시 실행**
→ 동작: `볼륨 100% 설정` → `URL 열기`(유튜브) 또는 `음악 재생`.
`.env`에 `IMESSAGE_TARGET`(아이폰 번호) 설정.

### 4. 매일 밤 실행
```bash
./scripts/tonight.sh
```
취침 전: **링 착용** 💍 · **아이폰 블루투스 OFF**(맥이 링 점유) · **아이폰 무음 OFF**.

## 주요 설정 (.env)
| 키 | 설명 | 기본 |
|----|------|------|
| `TARGET_SLEEP_HOURS` | 목표 수면 시간 | 8 |
| `CAP_TIME` | 안전 상한 시각(무조건 기상) | 09:00 |
| `POLL_INTERVAL_MIN` | 폴링 간격(분) | 10 |
| `HEALTHY_MODE` | 1이면 REM/깊은수면 조건 추가 | 0 |
| `REM_MIN_MIN` / `DEEP_MIN_MIN` | 건강모드 REM/깊은수면 목표(분) | 70 / 55 |
| `IMESSAGE_TARGET` | 알람 보낼 아이폰 번호 | — |

## 문서
- [USAGE.md](USAGE.md) — 상세 사용법 / 3중 안전장치 / 테스트
- [SETUP.md](SETUP.md) — 맥 셋업 / 인증키 추출
- [PRD.md](PRD.md) — 제품 요구사항

## 주의
- 개인용. `.env`, `key.hex`, `data/`(건강데이터)는 커밋 금지(gitignore).
- 수면단계(REM/깊은수면)는 **원시신호 기반 추정치**다(모델 없이 근사). 며칠 로그로 본인 기준 보정 권장.
- 비공식 BLE 프로토콜 — 펌웨어 업데이트로 동작이 바뀔 수 있음(그때도 폴백 알람은 상한 시각에 울림).
