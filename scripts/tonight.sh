#!/bin/bash
# 오늘 밤 이 하나로 전부 실행: 웹서버 + 야간 폴링 세션.
# 사전 조건: 링 착용, 아이폰 블루투스 OFF, 무음 OFF, 폴링 맥은 침대 근처.
# 사용법: ./scripts/tonight.sh [--tui]
#   맥 안 자게: caffeinate -s ./scripts/tonight.sh --tui
# 설정은 .env 로 오버라이드. WEB=0 이면 웹서버 안 켬. WEB_PORT 로 포트 변경(기본 8777).
set -uo pipefail
cd "$(dirname "$0")/.."

# .env 로드 (있으면)
if [ -f .env ]; then set -a; . ./.env; set +a; fi

PY=python3
[ -x .venv/bin/python ] && PY=.venv/bin/python
WEB="${WEB:-1}"
WEB_PORT="${WEB_PORT:-8777}"

WEB_PID=""
WR_PID=""
cleanup() {
  [ -n "$WR_PID" ]  && kill "$WR_PID"  2>/dev/null
  [ -n "$WEB_PID" ] && kill "$WEB_PID" 2>/dev/null
}
trap cleanup EXIT INT TERM

# 1) 웹서버 백그라운드 실행 (같은 와이파이에서 폰으로 상태 확인)
if [ "$WEB" = "1" ]; then
  WEB_PORT="$WEB_PORT" "$PY" scripts/web.py >logs/web.out 2>&1 &
  WEB_PID=$!
  sleep 1
  if kill -0 "$WEB_PID" 2>/dev/null; then
    echo "🌐 웹서버 실행 중 (pid $WEB_PID) — 접속주소는 아래 QR/배너 참고"
  else
    echo "⚠️ 웹서버 시작 실패 (logs/web.out 확인) — 폴링은 계속 진행"
    WEB_PID=""
  fi
fi

echo "=================================================="
echo " WakeReady 야간 세션"
echo "  목표 ${TARGET_SLEEP_HOURS:-8}h · 폴링 ${POLL_INTERVAL_MIN:-10}분 · 상한 ${CAP_TIME:-09:00}"
echo "  ⚠️ 링 착용 + 아이폰 블루투스 OFF + 무음 OFF"
echo "=================================================="

# 2) 폴링 세션. 백그라운드+wait 로 두어 Ctrl+C/종료 시 trap 이 둘 다 정리.
"$PY" scripts/wakeready.py "$@" &
WR_PID=$!
wait "$WR_PID"
