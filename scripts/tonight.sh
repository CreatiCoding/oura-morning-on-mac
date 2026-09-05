#!/bin/bash
# 오늘 밤 바로 실행: 자기 전에 이 스크립트를 켜두면 됨.
# 사전 조건: 링 착용, 아이폰 블루투스 OFF.
# 사용법: ./scripts/tonight.sh
# 설정은 .env 로 오버라이드 (TARGET_SLEEP_HOURS, POLL_INTERVAL_MIN, CAP_TIME, NTFY_TOPIC ...)
set -uo pipefail
cd "$(dirname "$0")/.."

# .env 로드 (있으면)
if [ -f .env ]; then set -a; . ./.env; set +a; fi

echo "=================================================="
echo " WakeReady 야간 세션"
echo "  목표 수면: ${TARGET_SLEEP_HOURS:-8}h | 폴링: ${POLL_INTERVAL_MIN:-10}분 | 상한: ${CAP_TIME:-09:00}"
echo "  푸시: $([ -n "${NTFY_TOPIC:-}" ] && echo "ntfy/${NTFY_TOPIC}" || echo '없음(로컬 사운드만)')"
echo "  ⚠️ 링 착용 + 아이폰 블루투스 OFF 확인!"
echo "=================================================="

# 파이썬 (venv 우선)
PY=python3
[ -x .venv/bin/python ] && PY=.venv/bin/python

exec "$PY" scripts/wakeready.py
