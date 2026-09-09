#!/usr/bin/env python3
"""맥미니 → tools.creco.dev/wakeready 로 읽기 전용 스냅샷 적재.

보내는 것: 웹(web.py)이 보여주는 것과 같은 데이터 — 밤별 공식/로컬 요약 + 5분 에폭(/api/nights)
과 세션 상태(status.json, 클라우드/로컬 선택 포함). 약 55KB. 링 원본(raw)은 보내지 않는다.
설정(.env): WAKEREADY_PUSH_URL, WAKEREADY_PUSH_TOKEN. 둘 중 하나라도 없으면 조용히 종료(코드 0).
호출: launchd com.wakeready.push(5분마다) + wakeready.py 가 폴링 성공 직후(best-effort).
"""
import json
import os
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"


def _env(key):
    v = os.environ.get(key, "")
    if v or not ENV.exists():
        return v
    for line in ENV.read_text().splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _load(path, name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def build_payload():
    se = _load(ROOT / "scripts" / "sleep_estimate.py", "se_push")
    web = _load(ROOT / "scripts" / "web.py", "web_push")     # 서버는 __main__ 가드라 실행 안 됨
    db = str(ROOT / "data" / "oura.db")
    status = json.loads(web.status_payload())                # 클라우드/로컬 선택·출처 라벨 포함
    status.pop("next_poll", None)                            # 원격에선 의미 없음
    return {"nights": se.nights_overview(db), "status": status}


def main():
    url, token = _env("WAKEREADY_PUSH_URL"), _env("WAKEREADY_PUSH_TOKEN")
    if not url or not token:
        return 0
    payload = build_payload()
    data = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    ts = datetime.now().strftime("%m-%d %H:%M:%S")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read() or b"{}")
        print(f"[{ts}] push ok  {len(data)//1024}KB  nights={body.get('nights')}  received_at={body.get('received_at')}")
        return 0
    except Exception as e:
        print(f"[{ts}] push 실패: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
