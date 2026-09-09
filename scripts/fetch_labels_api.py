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
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "training"
DB = ROOT / "data" / "oura.db"


ENV = ROOT / ".env"


def _env(key):
    """환경변수 → .env 순으로 읽는다(웹서버(launchd) 등 .env 를 안 거친 호출 대비)."""
    v = os.environ.get(key, "")
    if v or not ENV.exists():
        return v
    for line in ENV.read_text().splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _save_env(updates):
    lines = ENV.read_text().splitlines() if ENV.exists() else []
    out = [l for l in lines if not any(l.startswith(k + "=") for k in updates)]
    out += [f"{k}={v}" for k, v in updates.items()]
    ENV.write_text("\n".join(out) + "\n")
    os.chmod(ENV, 0o600)


def refresh_token():
    """액세스 토큰 만료(401) 시 OURA_REFRESH_TOKEN 으로 재발급해 .env 에 저장. 성공 시 새 토큰, 실패 시 ''."""
    import urllib.parse
    cid, csec, rt = _env("OURA_CLIENT_ID"), _env("OURA_CLIENT_SECRET"), _env("OURA_REFRESH_TOKEN")
    if not (cid and csec and rt):
        print("토큰 만료됐는데 리프레시 정보(OURA_CLIENT_ID/SECRET/REFRESH_TOKEN) 없음 → oura_oauth.py 재실행 필요")
        return ""
    data = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": rt,
                                   "client_id": cid, "client_secret": csec}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(
                "https://api.ouraring.com/oauth/token", data=data), timeout=20) as r:
            tok = json.loads(r.read())
    except Exception as e:
        print(f"토큰 갱신 실패: {e} → oura_oauth.py 재실행 필요"); return ""
    at = tok.get("access_token")
    if not at:
        print(f"토큰 갱신 응답에 access_token 없음: {tok}"); return ""
    _save_env({"OURA_API_TOKEN": at, "OURA_REFRESH_TOKEN": tok.get("refresh_token") or rt})
    os.environ["OURA_API_TOKEN"] = at
    print("[✓] 액세스 토큰 자동 갱신 → .env 저장")
    return at


TOKEN = _env("OURA_API_TOKEN")
API = "https://api.ouraring.com/v2/usercollection/sleep"
# Oura API sleep_phase_5_min 인코딩: 1=deep, 2=light, 3=rem, 4=awake
PHASE = {"1": "DEEP", "2": "LIGHT", "3": "REM", "4": "WAKE"}


def main():
    if not TOKEN:
        print("OURA_API_TOKEN 환경변수 필요 (OAuth 액세스 토큰 또는 PAT)."); sys.exit(2)
    end = sys.argv[2] if len(sys.argv) > 2 else str(date.today())
    start = sys.argv[1] if len(sys.argv) > 1 else str(date.today() - timedelta(days=7))
    url = f"{API}?start_date={start}&end_date={end}"
    token = TOKEN
    data = None
    for attempt in (1, 2):
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 1:
                token = refresh_token()
                if token:
                    continue
            print(f"API 호출 실패: {e}"); sys.exit(1)
        except Exception as e:
            print(f"API 호출 실패: {e}"); sys.exit(1)

    # 1) 클라우드 정규화 데이터 → 로컬 DB(wr_sleep_nights/epochs, source='cloud'). 낮잠 포함 전부.
    stored = 0
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("se", ROOT / "scripts" / "sleep_estimate.py")
        se = importlib.util.module_from_spec(spec); spec.loader.exec_module(se)
        for s in data.get("data", []):
            if se.store_cloud_night(str(DB), s) is not None:
                stored += 1
        print(f"[✓] 클라우드 수면 기록 {stored}건 → {DB} (wr_sleep_nights, source=cloud)")
    except Exception as e:
        print(f"DB 저장 실패(라벨 파일은 계속): {e}")

    # 2) 학습 라벨 파일 (본수면만)
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
