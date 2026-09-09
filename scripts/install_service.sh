#!/bin/bash
# 맥미니 상주화: 재부팅/매일 밤 자동 시작 (launchd LaunchAgent 2개).
#   - com.wakeready.web     : 웹서버 24시간 상주(KeepAlive, 부팅 시 자동)
#   - com.wakeready.session : 매일 밤 SESSION_HOUR 시에 야간 폴링 세션 시작
#   - com.wakeready.push    : 5분마다 읽기 전용 스냅샷을 tools.creco.dev 로 적재(.env 에 PUSH 설정 있을 때만 동작)
# 사용: ./scripts/install_service.sh [install|uninstall]  (기본 install)
#   야간 시작 시각: SESSION_HOUR=22 ./scripts/install_service.sh
# 주의: LaunchAgent 는 '로그인 세션'에서 돎 → 맥미니 자동 로그인 필요(BLE/iMessage/키체인).
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PY="$ROOT/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
HOUR="${SESSION_HOUR:-22}"
LA="$HOME/Library/LaunchAgents"
mkdir -p "$LA" "$ROOT/logs"

WEB_PLIST="$LA/com.wakeready.web.plist"
SES_PLIST="$LA/com.wakeready.session.plist"
PUSH_PLIST="$LA/com.wakeready.push.plist"

action="${1:-install}"
if [ "$action" = "uninstall" ]; then
  launchctl unload "$WEB_PLIST" 2>/dev/null || true
  launchctl unload "$SES_PLIST" 2>/dev/null || true
  launchctl unload "$PUSH_PLIST" 2>/dev/null || true
  rm -f "$WEB_PLIST" "$SES_PLIST" "$PUSH_PLIST"
  echo "제거 완료 (웹/세션 자동시작 해제)"; exit 0
fi

cat > "$WEB_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.wakeready.web</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string><string>$ROOT/scripts/web.py</string></array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$ROOT/logs/web.out</string>
  <key>StandardErrorPath</key><string>$ROOT/logs/web.out</string>
</dict></plist>
PLIST

cat > "$SES_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.wakeready.session</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/caffeinate</string><string>-s</string>
    <string>/bin/bash</string><string>$ROOT/scripts/tonight.sh</string></array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key><dict><key>WEB</key><string>0</string></dict>
  <key>StartCalendarInterval</key><dict>
    <key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>$ROOT/logs/session.out</string>
  <key>StandardErrorPath</key><string>$ROOT/logs/session.out</string>
</dict></plist>
PLIST

cat > "$PUSH_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.wakeready.push</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string><string>$ROOT/scripts/push_snapshot.py</string></array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>300</integer>
  <key>StandardOutPath</key><string>$ROOT/logs/push.out</string>
  <key>StandardErrorPath</key><string>$ROOT/logs/push.out</string>
</dict></plist>
PLIST

launchctl unload "$WEB_PLIST" 2>/dev/null || true
launchctl unload "$SES_PLIST" 2>/dev/null || true
launchctl unload "$PUSH_PLIST" 2>/dev/null || true
launchctl load "$WEB_PLIST"
launchctl load "$SES_PLIST"
launchctl load "$PUSH_PLIST"
echo "✅ 상주화 완료"
echo "  - 웹서버: 부팅 시 자동 + 24시간 KeepAlive (http://<맥>.local:8777)"
echo "  - 야간세션: 매일 ${HOUR}:00 자동 시작 (WEB=0, 폴링만)"
echo "  - 원격뷰 적재: 5분마다 tools.creco.dev/wakeready (.env 의 WAKEREADY_PUSH_URL/TOKEN 있을 때)"
echo ""
echo "재부팅에도 유지됨. 상태: launchctl list | grep wakeready"
echo "⚠️ 맥미니 자동로그인 + 슬립금지 권장:  sudo pmset -a sleep 0"
