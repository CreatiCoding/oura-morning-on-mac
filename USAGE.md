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
- 아이폰 **블루투스 OFF — 설정 앱 > Bluetooth 에서** (제어 센터 토글은 새벽 5시에 자동으로 다시 켜짐. iMessage는 와이파이라 무관)
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

## 판정 기준 (언제 깨우나)
우선순위 순서다. 위가 아래를 이긴다.
1. **안전 상한 `CAP_TIME`(기본 09:00)** — 무조건 알람. 링 연결이 밤새 끊겨도 여기서는 반드시 울린다.
2. **연결 실패 폴백** — 폴링 창 `MAX_FAILS_BEFORE_FALLBACK`(6)회 연속 실패 + 상한 30분 이내면 미리 알람.
3. **목표 충족** — 총시간 모드(기본): 링의 수면 구간 길이(`bedtime_period`, 깬 시간 포함) ≥ `TARGET_SLEEP_HOURS`(8h).
   건강 모드(`HEALTHY_MODE=1`): 실제 잔 시간 ≥ 목표 **그리고** 추정 REM ≥ `REM_MIN_MIN`(70분), 깊은 ≥ `DEEP_MIN_MIN`(55분).
   - 단, 그 순간 **깊은수면**이면 최대 `DEEP_WAIT_MAX_MIN`(20분) 더 기다렸다가 깨운다(수면 관성 회피).
4. **스마트 기상 창** — 목표 `WAKE_WINDOW_MIN`(30분) 전부터, 지금 단계가 얕은수면/REM/깸이면 조금 일찍 깨운다.
   깊은수면이면 창 안에서 `WINDOW_POLL_MIN`(5분) 간격으로 다시 보며 기다린다. `WAKE_WINDOW_MIN=0`이면 끔.
   "지금 단계"는 마지막 동기화 시점의 최근 10분(2 에폭) 다수 단계이며, 추정이 없으면 3의 총량 기준만 쓴다.
5. **지난 수면 가드** — 수면 종료가 `STALE_AFTER_HOURS`(3h) 이상 지났으면 지난 밤으로 보고 무시.

총량(REM 몇 분)은 추정 오차가 ±25분쯤이라 3의 건강 모드는 여유 있게 잡고, 4의 "지금 단계" 판정이 실제
기상 시점을 고르게 두는 구조다. 판정 로직 점검은 링 없이도 된다:
```bash
python3 scripts/wakeready.py --once --dry-run --simulate=7.7 --simulate-stage=DEEP   # 창 안·깊은수면 → 대기
python3 scripts/wakeready.py --once --dry-run --simulate=7.7 --simulate-stage=LIGHT  # 창 안·얕은수면 → 기상
python3 scripts/wakeready.py --once --dry-run --simulate=8.2 --simulate-stage=DEEP   # 목표 충족·깊은수면 → 최대 20분 대기
```

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
  출력한다(`models/train_report.json`). 5밤 기준 실측: 일치율 50% → 72%, REM/깊은 총분 절대오차 ~25분(깬 시간 ~38분).
- 모델 교체 가드: 휴리스틱보다 낫고 직전 모델의 검증 일치율보다 2%p 이상 나빠지지 않을 때만 교체.
  직전 모델은 `models/sleep_clf.prev.pkl`로 백업.
- 액세스 토큰이 만료(401)되면 `.env`의 리프레시 토큰으로 자동 재발급해 저장한다. 리프레시도 실패하면
  `oura_oauth.py`를 다시 돌리라는 안내가 `logs/labels.out`에 남는다.
- 라벨/모델은 내 데이터 → `data/`·`models/` gitignore.

### 데이터 3계층과 웹 표시 출처
`data/oura.db` 하나에 셋이 같이 있다.

| 계층 | 테이블 | 쓰는 쪽 | 내용 |
|---|---|---|---|
| 원본(raw) | `events` | `oura sync` | IBI·가속도·체온 등 링 이벤트, 링 시계 기준 |
| 로컬 정규화 | `wr_sleep_nights` / `wr_sleep_epochs` (`source='local'`) | 매 폴링·웹 | 5분 에폭 피처 + 추정 단계, `method`=model/heuristic |
| 클라우드 정규화 | 같은 테이블 (`source='cloud'`) | `fetch_labels_api.py` | 공식 단계·5분 HR/HRV·효율, 원본 JSON 보관 |

웹 UI 는 **그 밤의 클라우드 기록이 있으면 클라우드(☁️ 배지)**, 없으면 **로컬 추정(💍 배지, 모델/휴리스틱 표기)** 을
기본으로 보여 준다. 상단 필터 행에서 **날짜 이동(‹ › / 날짜 선택)** 과 **출처 전환(☁️ 공식 / 💍 로컬)** 이 되고,
선택은 주소 해시(`#d=2026-09-09&s=local`)에 남아 링크로 공유·복원된다. 화면 구성:
- 헤드라인 총수면 + 목표 미터 + 출처 배지 (최신 밤이면 "라이브" 점, 5초마다 갱신)
- 5분 히프노그램(깸/REM/얕은/깊은, 정각 눈금, 터치·키보드 크로스헤어 툴팁) + "표로 보기"
- 단계별 타일: 분·비율, 다른 출처와의 차이(기록 범위가 다르면 "공식은 10:10까지 기록" 식으로 안내)
- 심박수 5분 라인(최저/최고/평균)
- "지금 동기화"·세션 상태는 최신 밤(라이브)에서만 보임
밤중엔 클라우드에 오늘 밤 기록이 없으니 자연히 로컬이 보이고, 아침에 아이폰 앱이 동기화하면 클라우드로 바뀐다.
알람 판정은 표시와 무관하게 항상 로컬 추정으로 한다.

```bash
sqlite3 data/oura.db "select source,day,method,rem_sec/60,deep_sec/60,awake_sec/60 from wr_sleep_nights order by night_start_unix"
```

### 원격 읽기 전용 뷰 (tools.creco.dev/wakeready)
맥이 밀어 올리고 서버는 보여주기만 한다. 서버가 죽어도 알람에는 영향 없음.
- 맥: `scripts/push_snapshot.py`가 밤별 공식/로컬 요약 + 5분 에폭 + 세션 상태(약 100KB, 링 원본 제외)를
  `WAKEREADY_PUSH_URL`로 POST. launchd `com.wakeready.push`가 5분마다, 야간 폴링 성공 직후에도 한 번.
  `.env`에 `WAKEREADY_PUSH_URL`/`WAKEREADY_PUSH_TOKEN`이 없으면 아무것도 안 함. 로그는 `logs/push.out`.
- 서버: `CreatiCoding/tools.creco.dev` 저장소의 `services/web/src/app/wakeready/`. Dokploy `tools.creco.dev` 앱에
  볼륨 `/data`와 환경변수 `WAKEREADY_PUSH_TOKEN`, `WAKEREADY_DATA_DIR`가 설정돼 있고 main 푸시 시 자동 배포.
- UI 원본은 `web.py` 하나다. 화면을 바꾸면 `python3 scripts/export_readonly_ui.py <tools repo>/services/web/src/app/wakeready/ui.ts`
  로 다시 내보내서 그쪽에 커밋한다(동기화 버튼 제거·주소 치환은 스크립트가 함).
- 보기는 공개, 적재만 토큰 보호. 토큰 교체 시 Dokploy 환경변수와 `.env` 둘 다 바꾸고 재배포.
- 밤이 쌓일수록 정확해진다. 현재 휴리스틱(B)은 총·얕은수면·깬시간은 근접하나 REM↔깊은수면 구분이 약함 → 이 모델이 보완.
