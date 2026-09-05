#!/bin/bash
# WakeReady M0 셋업 스크립트 (맥미니용)
# 사용법: ./scripts/setup.sh
# 하는 일: Rust 설치 확인 → open_oura 클론/업데이트 → 빌드 → 스캔 안내
set -euo pipefail

OPEN_OURA_REPO="https://github.com/Th0rgal/open_oura.git"
OPEN_OURA_DIR="${OPEN_OURA_DIR:-$HOME/workspaces/open_oura}"
BIN_LINK="$(cd "$(dirname "$0")/.." && pwd)/bin/oura"

echo "==> [1/4] Rust 툴체인 확인"
if ! command -v cargo >/dev/null 2>&1; then
  if [ -x "$HOME/.cargo/bin/cargo" ]; then
    export PATH="$HOME/.cargo/bin:$PATH"
  else
    echo "    cargo가 없습니다. rustup으로 설치합니다..."
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
    export PATH="$HOME/.cargo/bin:$PATH"
  fi
fi
echo "    $(rustc --version)"

echo "==> [2/4] open_oura 클론/업데이트 ($OPEN_OURA_DIR)"
if [ -d "$OPEN_OURA_DIR/.git" ]; then
  git -C "$OPEN_OURA_DIR" pull --ff-only
else
  mkdir -p "$(dirname "$OPEN_OURA_DIR")"
  git clone "$OPEN_OURA_REPO" "$OPEN_OURA_DIR"
fi

echo "==> [3/4] 빌드 (release)"
(cd "$OPEN_OURA_DIR" && cargo build --release)

mkdir -p "$(dirname "$BIN_LINK")"
ln -sf "$OPEN_OURA_DIR/target/release/oura" "$BIN_LINK"
echo "    바이너리 링크: $BIN_LINK"

echo "==> [4/4] 완료! 다음 단계:"
cat <<'EOF'
    1. 링 스캔 테스트 (첫 실행 시 터미널 블루투스 권한 팝업 → 허용):
         ./bin/oura scan
       * 링이 아이폰과 연결 중이면 광고(advertising)를 안 해서 안 잡힐 수 있음.
         아이폰 블루투스를 잠시 끄고 재시도.
    2. 권한 팝업이 안 뜨고 실패하면:
         시스템 설정 > 개인정보 보호 및 보안 > Bluetooth > 터미널 앱 추가
EOF
