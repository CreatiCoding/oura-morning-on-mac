# WakeReady

Oura 링(Gen5)을 밤중에 직접 읽어, **"충분히/건강하게 잤을 때" 아이폰 알람을 울리는** 맥 상주형 기상 시스템.

공식 앱/클라우드는 실시간 기상 판정을 못 한다(데이터가 기상 후에야 올라옴). 그래서 [open_oura](https://github.com/Th0rgal/open_oura)로 **링과 BLE 직접 통신**해 밤중에 판정한다.

```
밤: 링(착용) ──BLE(읽기전용)──▶ 맥 ──판정──▶ iMessage "WAKEREADY"
                                                    ▶ 아이폰 단축어 → 노래/유튜브 🎵
```

- **클라우드 미접촉·읽기 전용** (공식 앱 무손상)
- **알람은 아이폰에서** — iMessage→단축어로 노래 재생 (앱 상주 불필요, 무료)
- **건강 수면 모드** — 총시간 + REM/깊은수면(추정) 충족 시 기상
- **3중 안전장치** — 목표충족 / 안전상한 시각 / 실패 폴백

## 시작하기

```bash
./scripts/setup.sh          # open_oura 빌드 + ./bin/oura 링크
cp .env.example .env        # 설정 채우기 (자세한 항목은 .env.example 주석 참고)
```

그다음 최초 1회:
1. **링 인증 키** — `.env`의 `OURA_AUTH_KEY`. 추출법은 [SETUP.md](SETUP.md).
2. **아이폰 알람 자동화** — 단축어 앱 → 자동화 → 메시지 `WAKEREADY` → 즉시 실행 → 볼륨100%+노래재생. 자세히는 [USAGE.md](USAGE.md).

## 실행

```bash
./scripts/tonight.sh                        # 야간 세션 시작 (실시간 상태 출력)
```
취침 전: 링 착용 💍 · 아이폰 블루투스 OFF · 무음 OFF.

테스트:
```bash
python scripts/wakeready.py --test-alarm            # 알람만 즉시 발동
python scripts/wakeready.py --once --simulate=8.2   # 링 없이 판정 로직만 확인
```
전체 플래그·상세는 [USAGE.md](USAGE.md).

## 문서
- [USAGE.md](USAGE.md) — 상세 사용법 / 설정 / 테스트 플래그
- [SETUP.md](SETUP.md) — 맥 셋업 / 인증키 추출
- [PRD.md](PRD.md) — 제품 요구사항

## 주의
- 개인용. `.env`·`key.hex`·`data/`(건강데이터)는 커밋 금지(gitignore).
- 수면단계(REM/깊은수면)는 **원시신호 기반 추정치**다(모델 없이 근사). 며칠 로그로 보정 권장.
- 비공식 BLE 프로토콜 — 펌웨어 업데이트로 바뀔 수 있음(그래도 폴백 알람은 상한 시각에 울림).
