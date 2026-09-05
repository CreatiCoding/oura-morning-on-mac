#!/bin/bash
# 맥에서 FaceTime 오디오 전화 자동 발신 → 아이폰을 울림(연락처 개별 벨소리 적용).
# 사용법: FACETIME_TARGET=<전화번호 또는 AppleID이메일> ./facetime_call.sh [울림초]
# 주의: 실행 주체(터미널/스크립트)에 '손쉬운 사용(Accessibility)' 권한 필요.
set -uo pipefail

TARGET="${FACETIME_TARGET:-}"
RING="${1:-40}"
if [ -z "$TARGET" ]; then echo "FACETIME_TARGET 환경변수 필요"; exit 2; fi

echo "[$(date '+%H:%M:%S')] FaceTime 발신 → $TARGET (${RING}초 울림)"

# FaceTime 오디오 URL 열기 → 통화 버튼 자동 클릭
osascript <<EOF
open location "facetime-audio://$TARGET"
delay 4
tell application "System Events"
  tell process "FaceTime"
    set frontmost to true
    delay 1
    -- macOS 버전마다 버튼 레이블/구조가 달라 여러 방법 시도
    set clicked to false
    try
      click button "Call" of window 1
      set clicked to true
    end try
    if not clicked then
      try
        click button "FaceTime" of window 1
        set clicked to true
      end try
    end if
    if not clicked then
      -- 마지막 수단: return 키
      keystroke return
    end if
  end tell
end tell
EOF

sleep "$RING"

# 울림 종료(응답 안 하면 끊기 위해 FaceTime 종료)
osascript -e 'tell application "FaceTime" to quit' 2>/dev/null || true
echo "[$(date '+%H:%M:%S')] FaceTime 발신 종료"
