# CLAUDE.md — 이 저장소에서 작업하는 에이전트를 위한 안내

이 파일은 **Claude Code가 이 저장소를 클론한 사용자의 세팅/사용을 도울 때** 참고하는 가이드다.
사용자가 "이거 세팅 도와줘"라고 하면 아래 흐름대로 안내하라.

## 이 프로젝트가 뭔가
**WakeReady** — Oura 링을 밤중에 BLE로 직접 읽어(open_oura), "충분히/건강하게 잤을 때" 아이폰에서
노래로 깨우는 맥 상주형 스마트 알람. 클라우드 미접촉·읽기 전용·개인용.

전체 개요는 [README.md](README.md), 설계 배경은 [PRD.md](PRD.md), 상세 사용법은 [USAGE.md](USAGE.md),
맥 셋업/인증키 추출은 [SETUP.md](SETUP.md). **먼저 이 문서들을 읽고 사용자 상황에 맞춰 안내하라.**

## 아키텍처 / 코드 지도
```
링(착용) ─BLE(읽기전용)→ 맥[open_oura] → SQLite(data/oura.db)
   → sleep_estimate.py(REM/깊은수면 추정) → wakeready.py(판정)
   → alarm.sh → iMessage "WAKEREADY" → 아이폰 단축어(노래 재생) → ntfy ACK
   웹: wakeready→status.json→web.py(:8777)→폰 브라우저(QR)
```
- `scripts/tonight.sh` — **진입점**. web.py + wakeready.py 를 함께 실행, 종료 시 정리.
- `scripts/wakeready.py` — 야간 폴링 루프 + 판정 + 알람 트리거. `.env` 자동 로드. `--once/--test-alarm/--dry-run/--simulate/--poll/--tui/--verbose` 플래그.
- `scripts/sleep_estimate.py` — HR/HRV/모션 → WAKE/LIGHT/DEEP/REM 휴리스틱 추정(공식 모델 없음).
- `scripts/alarm.sh` — iMessage 트리거(+ntfy ACK 확인·폴백). 맥 스피커는 기본 off.
- `scripts/web.py` — stdlib 웹서버. status.json 표시 + "지금 동기화" 버튼(→sync_request 플래그).
- `scripts/_qr.py` — 접속 QR(터미널). `scripts/setup.sh` — open_oura 빌드.
- `.env`(gitignore) — 모든 설정/비밀. `.env.example` 참고.

## 세팅 안내 순서 (사용자에게 이 순서로)
1. **환경**: 침대에서 BLE 닿는 맥(밤새 켬), Oura 링, 아이폰(맥과 같은 와이파이·같은 Apple ID).
2. **빌드**: `./scripts/setup.sh` → `pip install -r requirements.txt`(선택) → `cp .env.example .env`.
3. **인증키(가장 어려움)**: iPhone 암호화 백업에서 `assa.sqlite`의 `ringconfiguration.auth_key` 추출 → `key.hex`/`.env OURA_AUTH_KEY`. 절차는 [SETUP.md](SETUP.md). 검증: `./bin/oura --key-file key.hex info`(배터리 나오면 성공).
4. **아이폰 알람 자동화**: 단축어 앱 → 자동화 → 메시지 포함 `WAKEREADY` → **즉시 실행** → 볼륨100%+음악/URL. `.env`에 `IMESSAGE_TARGET`. 상세는 [USAGE.md](USAGE.md).
5. **실행**: `caffeinate -s ./scripts/tonight.sh --tui`.

## 사람이 직접 해야 하는 것 (에이전트가 대신 못 함)
- iPhone 암호화 백업 켜기(Finder) + 백업 암호 입력
- 아이폰 단축어/자동화 생성 (GUI)
- 취침 시: 링 착용, **아이폰 블루투스 OFF**, 무음 OFF
- 이 작업들은 사용자에게 명확히 요청하고, 결과(예: `oura info` 출력)를 확인 후 다음 단계로.

## 반드시 지킬 안전/프라이버시 규칙
- **읽기 전용 원칙**: `oura` 의 `pair`/`factory-reset`/`--include-state`/`--include-danger` 등
  상태변경·파괴 명령은 **절대 실행 금지**. sync/sleep-analyze/info/latest 등 읽기만.
- **비밀/건강데이터 커밋 금지**: `.env`, `key.hex`, `*.hex`, `data/`, `logs/` 는 `.gitignore` 처리됨.
  커밋 전 diff에 auth_key(hex 32자)·전화번호·Apple ID·시리얼·MAC·IP가 없는지 확인.
- 외부 전송은 알람 트리거(iMessage/ntfy)뿐. 그 외 건강데이터는 맥 로컬에만.

## 알아두면 좋은 함정 (이미 코드에 반영됨)
- **BLE 연속 재연결 취약**: `sleep-analyze` 직후 곧바로 `sync` 하면 재광고 전이라 실패.
  → 명령 사이 딜레이 + 창 내 성공까지 재시도(poll_window)로 완화.
- **지난밤 데이터 오발동**: bedtime_period엔 날짜가 없음. 수면 '종료'가 지금으로부터
  `STALE_AFTER_HOURS`+ 지났으면 지난 수면으로 보고 알람 미발동(오늘 새 수면만 판정).
- **연결 간헐성**: 착용 중 링 광고가 드물어 단발 스캔 성공률이 낮음 → 창 내 반복으로 보완.
  맥은 침대 가까이. 밤새 BLE가 끊겨도 안전 상한 시각엔 반드시 알람.
- **REM/깊은수면은 추정치**: 공식 SleepNet 모델(서버 키 필요)이 없어 원시신호 휴리스틱.
  임계값(`REM_MIN_MIN`/`DEEP_MIN_MIN`)은 며칠 로그로 사용자별 보정 권장.

## 빠른 확인/테스트 명령
```bash
./bin/oura --key-file key.hex info                  # 링 연결/인증
python3 scripts/wakeready.py --once                 # 현재 수면 1회 확인
python3 scripts/wakeready.py --test-alarm           # 알람만 발동
python3 scripts/wakeready.py --once --simulate=8.2 --dry-run  # 링/알람 없이 판정 로직
```

## 흔한 문제
- `no matching Oura ring found` → 아이폰 BT OFF? 링 착용? 맥 근처? (창 내 자동 재시도)
- 아이폰 노래 안 울림 → 단축어 "즉시 실행"·트리거단어 `WAKEREADY`·무음 OFF 확인
- 웹 안 열림 → tonight.sh 실행 중(웹서버 동반)·같은 와이파이 확인
- "지금 동기화" 무반응 → 세션이 구버전이면 재시작(git pull 후). 폴링 중이면 현재 시도 후 반영.

## 커밋 규칙
- 개인정보/비밀 없는지 확인 후 커밋. 커밋 메시지는 한글, Impact/Decision 등 간결히.
