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
