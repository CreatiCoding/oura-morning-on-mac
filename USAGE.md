# WakeReady 사용법

Oura 링 데이터로 "충분히 잤을 때" 아이폰 알람(노래/유튜브)을 울리는 야간 세션.

## 아키텍처

```
밤: 링(착용) ──BLE(읽기전용)──▶ 맥(open_oura)
                                  │ 10분마다 sleep-analyze + sync
                                  │ 최신 bedtime_period.duration_hours 판정
                                  ▼ 목표(8h) 도달 또는 상한(09:00)
                            iMessage "WAKEREADY" ──▶ 아이폰 단축어 자동화
                                                      ▶ 볼륨100% + 노래/유튜브 🎵
```

## 최초 1회 설정

### 1. 링 인증 키 (M1, 완료됨)
`key.hex` + `.env`의 `OURA_AUTH_KEY`. (iPhone 암호화 백업 → `assa.sqlite`의 `ringconfiguration.auth_key`에서 추출. 재현법은 memory 참고)

### 2. 아이폰 단축어 자동화 (알람 재생)
1. **단축어(동작) 생성**: 볼륨 100% 설정 → `URL 열기`(유튜브 링크) 또는 `음악 재생`(Apple Music 곡)
2. **자동화 생성**: 단축어 앱 → 자동화 → **메시지** → "메시지 포함 내용" = `WAKEREADY` → **즉시 실행** → 1의 동작 실행
3. `.env`에 `IMESSAGE_TARGET`(아이폰 번호), `WAKE_TRIGGER_WORD=WAKEREADY`

### 3. 설정 파일
`cp .env.example .env` 후 값 채우기. 주요: `TARGET_SLEEP_HOURS`, `CAP_TIME`, `POLL_INTERVAL_MIN`.

## 매일 밤 실행

**취침 전 준비:**
- 링 **착용** 💍
- 아이폰 **블루투스 OFF** (맥이 링을 점유하도록. iMessage는 와이파이라 무관)
- 아이폰 **무음 스위치 OFF** (알람 소리 나도록)

**세션 시작:**
```bash
./scripts/tonight.sh
```
→ 목표 수면 도달 시(또는 상한 시각) 아이폰에서 노래 재생. 로그: `logs/wakeready.jsonl`

## 3중 안전장치 (알람 실패 방지)
1. 목표 수면 충족 → 알람
2. 안전 상한 시각(`CAP_TIME`) → 무조건 알람
3. 연결 실패 누적 + 상한 임박 → 조기 폴백 알람
4. 스크립트 예외 → 폴백 알람

## 실시간 출력
세션 중 매 폴링마다 터미널에 누적 수면·품질이 표시된다:
```
💤 지금까지 6.5h  [████████████████░░░░] 목표 8h까지 1.5h
🧠 😴 잘 자는 중 (효율 91%) | REM 92분(21%) · 깊은 78분(18%) · 깬 20분
수면 판정 — ⏳ 부족: REM 대기 ...
다음 폴링 07:20:00 (약 10분 후)
```

## 테스트/디버그 플래그
```bash
python scripts/wakeready.py --once            # 1회만 폴링→현재 상태/판정 출력 후 종료
python scripts/wakeready.py --test-alarm      # 즉시 알람만 발동(알람 경로 테스트)
python scripts/wakeready.py --dry-run         # 루프 정상, 단 조건 충족 시 실제 알람 대신 로그
python scripts/wakeready.py --simulate=8.5    # 수면시간 가정(링 없이 판정 로직 테스트)
python scripts/wakeready.py --poll=30         # 폴링 간격 30초로 강제(빠른 테스트)
python scripts/wakeready.py --verbose         # 상세 로그
python scripts/wakeready.py --tui             # 예쁜 TUI 카드로 실시간 표시
# 조합 예: 링/알람 없이 판정만 빠르게
python scripts/wakeready.py --once --simulate=8.2 --dry-run
```

## 수동 테스트
```bash
set -a; . ./.env; set +a
IMESSAGE_REPEAT=1 bash scripts/alarm.sh "테스트"   # 알람만
./bin/oura --key-file key.hex info                 # 링 연결/인증
```

## 다음(M5): launchd 상주화
매일 지정 시각 자동 시작은 launchd 등록으로. (미구현)

## 개인화 수면단계 모델 (#2, 선택 — 정확도 향상)
Oura의 공식 히프노그램(내 데이터)을 정답으로 내 원시신호에 맞춰 분류기를 학습한다.
독점 모델/키는 안 건드리며, 학습 목표가 Oura 출력이라 잘 되면 근접해진다.

**자동**: `.env`에 `OURA_API_TOKEN`이 있으면 `tonight.sh`가 시작할 때 최근 14일 라벨을 받고
(`logs/labels.out`) 바로 재학습한다(`logs/train.out`). 검증에서 휴리스틱보다 나을 때만
`models/sleep_clf.pkl`을 저장하고, 그 뒤 폴링부터 자동 사용된다.

```bash
# 토큰 발급(1회): cloud.ouraring.com 에 OAuth 앱 만들고 client_id/secret 을 .env 에 → 브라우저 승인
python3 scripts/oura_oauth.py
# 수동으로 돌릴 때
.venv/bin/python scripts/fetch_labels_api.py 2026-09-01 2026-09-09   # 라벨 → data/training/
.venv/bin/python scripts/train_model.py                              # 검증 리포트 + 모델 저장
.venv/bin/python scripts/train_model.py --no-save                    # 리포트만
```
- 정렬: 링 `time_sync` 이벤트로 링 시계↔실제 시각을 환산해(오차 <1분) 공식 5분 히프노그램과
  에폭 단위로 절대시각 정렬. 신호는 5분 HRV 요약이 아니라 밤새 연속 기록되는 **IBI(박동 간격)·
  움직임·체온 원본**에서 뽑는다(아티팩트 제거 후 RMSSD 는 링 자체값과 근접).
- 리포트: "밤 하나 빼기" 검증으로 밤마다 휴리스틱 vs 모델의 에폭 일치율과 REM/깊은/깬 총분 오차를
  출력한다(`models/train_report.json`). 5밤 기준 실측: 일치율 50% → 66%, 총분 오차는 아직 ±40분대.
- 라벨/모델은 내 데이터 → `data/`·`models/` gitignore.

### 데이터 3계층과 웹 표시 출처
`data/oura.db` 하나에 셋이 같이 있다.

| 계층 | 테이블 | 쓰는 쪽 | 내용 |
|---|---|---|---|
| 원본(raw) | `events` | `oura sync` | IBI·가속도·체온 등 링 이벤트, 링 시계 기준 |
| 로컬 정규화 | `wr_sleep_nights` / `wr_sleep_epochs` (`source='local'`) | 매 폴링·웹 | 5분 에폭 피처 + 추정 단계, `method`=model/heuristic |
| 클라우드 정규화 | 같은 테이블 (`source='cloud'`) | `fetch_labels_api.py` | 공식 단계·5분 HR/HRV·효율, 원본 JSON 보관 |

웹 UI 는 **그 밤의 클라우드 기록이 있으면 클라우드(☁️ 배지)**, 없으면 **로컬 추정(💍 배지, 모델/휴리스틱 표기)** 을
보여주고, 클라우드일 땐 로컬 추정값을 작게 같이 보여 준다(모델이 얼마나 맞는지 매일 확인용).
밤중엔 클라우드에 오늘 밤 기록이 없으니 자연히 로컬이 보이고, 아침에 아이폰 앱이 동기화하면 클라우드로 바뀐다.
알람 판정은 표시와 무관하게 항상 로컬 추정으로 한다.

```bash
sqlite3 data/oura.db "select source,day,method,rem_sec/60,deep_sec/60,awake_sec/60 from wr_sleep_nights order by night_start_unix"
```
- 밤이 쌓일수록 정확해진다. 현재 휴리스틱(B)은 총·얕은수면·깬시간은 근접하나 REM↔깊은수면 구분이 약함 → 이 모델이 보완.
