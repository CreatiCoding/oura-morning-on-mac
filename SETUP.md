# 맥미니 셋업 가이드 (M0)

## 1. 이 저장소 클론 + 셋업

```bash
git clone <이 저장소 URL> ~/workspaces/oura-morning-on-mac
cd ~/workspaces/oura-morning-on-mac
./scripts/setup.sh
```

스크립트가 자동으로 처리하는 것:
- Rust(rustup) 설치 확인, 없으면 설치
- [open_oura](https://github.com/Th0rgal/open_oura) 클론 (`~/workspaces/open_oura`) 및 release 빌드
- `./bin/oura` 심볼릭 링크 생성

## 2. 블루투스 권한 + 링 스캔

```bash
./bin/oura scan
```

- 첫 실행 시 macOS 블루투스 권한 팝업 → **허용**
- 팝업 없이 실패하면: 시스템 설정 → 개인정보 보호 및 보안 → Bluetooth → 사용 중인 터미널 앱 추가
- **링이 아이폰과 연결 중이면 광고를 안 해서 스캔에 안 잡힐 수 있음** → 아이폰 블루투스를 잠시 끄고 재시도
- 성공 기준: `Oura` 이름의 기기가 목록에 표시

## 3. 다음 단계 (M1) 사전 지식

인증이 필요한 명령(배터리, 이벤트 스트림, 심박)은 링의 **16바이트 auth key**가 필요:

```bash
./bin/oura --key-file key.hex info
```

⚠️ **주의**: 이미 공식 앱에 온보딩된 링의 키는 공식 앱(폰)의 Realm DB에 저장되어 있음.
- 루팅된 Android라면 `tools/android_oura_key_extract.py`로 추출 가능
- iPhone만 있는 경우 키 추출 경로가 검증되어 있지 않음 → M1에서 해결 방법 조사 필요
- `oura pair`는 **팩토리 리셋된 링 전용** — 기존 링에 함부로 실행하면 공식 앱 페어링이 깨질 수 있으므로 금지 (PRD 읽기 전용 원칙)
- `key.hex`는 절대 커밋 금지 (`.gitignore` 등록됨)
