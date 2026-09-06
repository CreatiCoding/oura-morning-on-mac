#!/usr/bin/env python3
"""#2 라벨 경량 수집기 — 공식 Oura API로 '내 수면 히프노그램'을 받아 저장.

25GB 백업 대신, 공식 API 한 콜로 내 정답 라벨을 매일 받는다(내 데이터).
학습 목적이라 실시간 기상엔 안 쓰이며, 런타임 BLE는 여전히 클라우드 미접촉.

토큰: 환경변수 OURA_API_TOKEN (OAuth 액세스 토큰 또는 기존 PAT).
      PAT 신규발급은 2025-12 중단 → 없으면 OAuth 앱 필요(README 시작부 참고).
사용: OURA_API_TOKEN=xxx python3 fetch_labels_api.py [start_date] [end_date]
      (날짜 없으면 최근 7일). 출력: data/training/labels_api_<day>.json
"""
import json
import os
import sys
import urllib.request
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "training"
TOKEN = os.environ.get("OURA_API_TOKEN", "")
API = "https://api.ouraring.com/v2/usercollection/sleep"
# Oura API sleep_phase_5_min 인코딩: 1=deep, 2=light, 3=rem, 4=awake
PHASE = {"1": "DEEP", "2": "LIGHT", "3": "REM", "4": "WAKE"}


def main():
    if not TOKEN:
        print("OURA_API_TOKEN 환경변수 필요 (OAuth 액세스 토큰 또는 PAT)."); sys.exit(2)
    end = sys.argv[2] if len(sys.argv) > 2 else str(date.today())
    start = sys.argv[1] if len(sys.argv) > 1 else str(date.today() - timedelta(days=7))
    url = f"{API}?start_date={start}&end_date={end}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
    except Exception as e:
        print(f"API 호출 실패: {e}"); sys.exit(1)

    OUT.mkdir(parents=True, exist_ok=True)
    saved = 0
    for s in data.get("data", []):
        hyp = s.get("sleep_phase_5_min")
        if not hyp or (s.get("total_sleep_duration") or 0) < 3600:
            continue
        stages = [PHASE.get(ch, "LIGHT") for ch in hyp]
        rec = {
            "day": s.get("day"),
            "bedtime_start": s.get("bedtime_start"),
            "bedtime_end": s.get("bedtime_end"),
            "epoch_sec": 300,                # API는 5분 해상도
            "stages": stages,
            "official_min": {
                "REM": round((s.get("rem_sleep_duration") or 0) / 60),
                "DEEP": round((s.get("deep_sleep_duration") or 0) / 60),
                "LIGHT": round((s.get("light_sleep_duration") or 0) / 60),
                "WAKE": round((s.get("awake_time") or 0) / 60),
            },
        }
        fn = OUT / f"labels_api_{s.get('day')}.json"
        fn.write_text(json.dumps(rec, ensure_ascii=False))
        saved += 1
        print(f"[✓] {fn.name}  {rec['official_min']}")
    print(f"\n{saved}밤 라벨 저장 → {OUT}  (train_model.py 로 학습)")


if __name__ == "__main__":
    main()
