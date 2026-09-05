#!/bin/bash
# WakeReady 알람 발동.
# 최우선(권장): iMessage 로 트리거 단어 발송 → 아이폰 단축어 자동화가 노래/유튜브 재생.
#   (아이폰 앱 포그라운드 불필요, 기기 소모 없음, 풀 트랙 가능, 무료)
# 폴백: Pushcut execute > 맥 LOCAL_SONG > Pushcut/ntfy 알림 > 맥 시스템사운드.
# 사용법: ./alarm.sh "사유 메시지"
# 환경변수:
#   IMESSAGE_TARGET     아이폰 번호/AppleID (예: +8210...) → iMessage 로 WAKE_TRIGGER_WORD 발송. 최우선.
#   WAKE_TRIGGER_WORD   아이폰 자동화가 감지할 단어 (기본 "WAKEREADY")
#   IMESSAGE_REPEAT     재전송 횟수 (기본 3, 첫 전송 놓침 대비)
#   IMESSAGE_GAP_SEC    재전송 간격 (기본 20초)
#   PUSHCUT_EXEC_URL    Pushcut execute URL (대안 노래 트리거)
#   LOCAL_SONG          맥에서 재생할 노래 파일 (afplay). 서버가 침실 근처일 때만 의미.
#   PUSHCUT_WEBHOOK / NTFY_TOPIC   알림 폴백
#   ALARM_SECONDS(120) / ALARM_REPEAT_SEC(5) / LOCAL_SOUND(0)
set -uo pipefail

REASON="${1:-WakeReady alarm}"
SECONDS_TO_RING="${ALARM_SECONDS:-120}"
REPEAT="${ALARM_REPEAT_SEC:-5}"
LOCAL_SOUND="${LOCAL_SOUND:-0}"
TRIGGER="${WAKE_TRIGGER_WORD:-WAKEREADY}"

echo "[$(date '+%H:%M:%S')] ALARM: $REASON"
END=$(( $(date +%s) + SECONDS_TO_RING ))
sent=0

send_imessage() {
  osascript <<OSA 2>/dev/null
tell application "Messages"
  set svc to 1st account whose service type = iMessage
  set b to participant "$1" of svc
  send "$2" to b
end tell
OSA
}

# 0) iMessage 트리거 (최우선) — 아이폰 단축어가 노래/유튜브 재생
if [ -n "${IMESSAGE_TARGET:-}" ]; then
  reps="${IMESSAGE_REPEAT:-3}"; gap="${IMESSAGE_GAP_SEC:-20}"
  echo "  iMessage 트리거 '$TRIGGER' → $IMESSAGE_TARGET (${reps}회)"
  for i in $(seq 1 "$reps"); do
    send_imessage "$IMESSAGE_TARGET" "$TRIGGER" && echo "    전송 $i/$reps" || echo "    전송 $i 실패"
    [ "$i" -lt "$reps" ] && sleep "$gap"
  done
fi

# 0b) 대안 노래 트리거들
if [ -n "${PUSHCUT_EXEC_URL:-}" ]; then
  curl -s -m 10 -X POST "$PUSHCUT_EXEC_URL" >/dev/null 2>&1 \
    && echo "  Pushcut execute 호출됨" || echo "  Pushcut execute 실패"
fi
if [ -n "${LOCAL_SONG:-}" ] && [ -f "$LOCAL_SONG" ]; then
  echo "  맥에서 노래 재생: $LOCAL_SONG"
  osascript -e "set volume output muted false" 2>/dev/null
  osascript -e "set volume output volume $(( ${ALARM_VOLUME:-8} * 10 ))" 2>/dev/null
  ( while [ "$(date +%s)" -lt "$END" ]; do afplay "$LOCAL_SONG" 2>/dev/null; done ) &
fi

# iMessage/노래 트리거가 하나라도 있으면 푸시 알림 반복은 생략 (중복 방지)
if [ -n "${IMESSAGE_TARGET:-}${PUSHCUT_EXEC_URL:-}${LOCAL_SONG:-}" ]; then
  echo "[$(date '+%H:%M:%S')] 알람 트리거 완료"
  exit 0
fi

push_pushcut() {
  # Pushcut 웹훅: title/text 를 쿼리로 전달 (알림 정의의 소리 사용)
  curl -s -m 8 -X POST "$PUSHCUT_WEBHOOK" \
    -H "Content-Type: application/json" \
    -d "{\"title\":\"⏰ 기상 시간!\",\"text\":\"$1\"}" >/dev/null 2>&1
}
push_ntfy() {
  curl -s -m 8 \
    -H "Title: ⏰ 기상 시간!" -H "Priority: urgent" -H "Tags: alarm_clock" \
    -d "$1" "https://ntfy.sh/${NTFY_TOPIC}" >/dev/null 2>&1
}

if [ -n "${PUSHCUT_WEBHOOK:-}" ]; then
  METHOD="Pushcut"
elif [ -n "${NTFY_TOPIC:-}" ]; then
  METHOD="ntfy"
else
  METHOD="local"; LOCAL_SOUND=1
fi
echo "  알림 수단: $METHOD"

# 반복 발송
if [ "$METHOD" != "local" ]; then
  while [ "$(date +%s)" -lt "$END" ]; do
    sent=$((sent + 1))
    if [ "$METHOD" = "Pushcut" ]; then push_pushcut "$REASON (#$sent)" || true
    else push_ntfy "$REASON (#$sent)" || true; fi
    sleep "$REPEAT"
  done
  echo "[$(date '+%H:%M:%S')] $METHOD 푸시 ${sent}회 발송 완료"
fi

# (옵션/폴백) 맥 스피커
if [ "$LOCAL_SOUND" = "1" ]; then
  SOUND="${ALARM_SOUND:-/System/Library/Sounds/Sosumi.aiff}"
  [ -f "$SOUND" ] || SOUND="/System/Library/Sounds/Ping.aiff"
  osascript -e "set volume output muted false" 2>/dev/null
  osascript -e "set volume output volume $(( ${ALARM_VOLUME:-8} * 10 ))" 2>/dev/null
  E2=$(( $(date +%s) + 30 ))
  while [ "$(date +%s)" -lt "$E2" ]; do afplay "$SOUND" 2>/dev/null; sleep 1; done
fi

echo "[$(date '+%H:%M:%S')] 알람 종료"
