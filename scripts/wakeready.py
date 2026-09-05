#!/usr/bin/env python3
"""WakeReady 야간 세션 러너.

주기적으로 링에 수면 분석을 시키고(sleep-analyze --force) 이벤트를 sync한 뒤,
최신 bedtime_period 의 duration_hours 를 읽어 목표 수면시간 도달 시 알람을 울린다.

3중 안전장치:
  1) 목표 수면 도달 → 알람 (정상 기상)
  2) 안전 상한 시각 도달 → 무조건 알람 (폴백)
  3) 연속 연결 실패가 누적되고 상한 시각이 가까우면 → 조기 폴백 알람

읽기 전용 원칙: sync / sleep-analyze 만 사용. 상태변경/파괴 명령 없음.
설정은 환경변수(.env) 로 오버라이드 가능.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# ---- 설정 (환경변수로 오버라이드) ----
ROOT = Path(__file__).resolve().parent.parent
OURA_BIN = os.environ.get(
    "OURA_BIN",
    str(Path.home() / "workspaces/toy-projects/open_oura/target/release/oura"),
)
KEY_FILE = os.environ.get("KEY_FILE", str(ROOT / "key.hex"))
DB = os.environ.get("DB", str(ROOT / "data" / "oura.db"))
LOG_DIR = Path(os.environ.get("LOG_DIR", str(ROOT / "logs")))
ALARM_SH = str(ROOT / "scripts" / "alarm.sh")

TARGET_SLEEP_HOURS = float(os.environ.get("TARGET_SLEEP_HOURS", "8"))
POLL_INTERVAL_MIN = float(os.environ.get("POLL_INTERVAL_MIN", "10"))
CAP_TIME = os.environ.get("CAP_TIME", "09:00")          # HH:MM, 안전 상한
SCAN_TIMEOUT = os.environ.get("SCAN_TIMEOUT", "25")
MAX_FAILS_BEFORE_FALLBACK = int(os.environ.get("MAX_FAILS_BEFORE_FALLBACK", "6"))


def log(msg, **fields):
    ts = datetime.now().isoformat(timespec="seconds")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"ts": ts, "msg": msg, **fields}
    with open(LOG_DIR / "wakeready.jsonl", "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def cap_datetime():
    """CAP_TIME 을 오늘/내일 중 '다음' 시각으로 해석."""
    hh, mm = map(int, CAP_TIME.split(":"))
    now = datetime.now()
    cap = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if cap <= now:
        cap += timedelta(days=1)
    return cap


def run_oura(*args, capture=True):
    cmd = [OURA_BIN, "--scan-timeout", SCAN_TIMEOUT, "--key-file", KEY_FILE,
           "--db", DB, *args]
    return subprocess.run(cmd, capture_output=capture, text=True, timeout=180)


def poll_once():
    """sleep-analyze + sync 후 최신 bedtime_period duration_hours 반환. 실패 시 None."""
    try:
        run_oura("sleep-analyze", "--force")  # 링이 분석을 돌리도록 요청(best-effort)
    except Exception as e:
        log("sleep-analyze 실패(무시)", error=str(e))
    try:
        r = run_oura("sync")
        if r.returncode != 0:
            log("sync 실패", stderr=(r.stderr or "")[-300:])
            return None
    except Exception as e:
        log("sync 예외", error=str(e))
        return None
    return latest_sleep_hours()


def latest_sleep_hours():
    """DB에서 가장 최근 bedtime_period 의 duration_hours 를 읽는다."""
    import sqlite3
    try:
        con = sqlite3.connect(DB)
        row = con.execute(
            "SELECT decoded_json FROM events WHERE name='bedtime_period' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        con.close()
        if not row or not row[0]:
            return None
        return float(json.loads(row[0]).get("duration_hours"))
    except Exception as e:
        log("DB 읽기 실패", error=str(e))
        return None


def fire_alarm(reason):
    log("ALARM 발동", reason=reason)
    try:
        subprocess.run(["bash", ALARM_SH, reason], timeout=200)
    except Exception as e:
        # 알람 스크립트마저 실패하면 최후의 수단
        log("alarm.sh 실패 — say 폴백", error=str(e))
        os.system('say "Wake up. WakeReady fallback alarm." 2>/dev/null')


def main():
    cap = cap_datetime()
    log("세션 시작", target_hours=TARGET_SLEEP_HOURS, poll_min=POLL_INTERVAL_MIN,
        cap_time=cap.isoformat(timespec="minutes"), db=DB)

    fails = 0
    while True:
        now = datetime.now()

        # 안전장치 2: 상한 시각 도달
        if now >= cap:
            fire_alarm(f"안전 상한 시각({CAP_TIME}) 도달 — 무조건 기상")
            break

        hours = poll_once()
        if hours is None:
            fails += 1
            log("폴링 실패", consecutive_fails=fails)
            # 안전장치 3: 실패 누적 + 상한 30분 이내면 조기 폴백
            if fails >= MAX_FAILS_BEFORE_FALLBACK and (cap - now) <= timedelta(minutes=30):
                fire_alarm("연결 실패 누적 + 상한 임박 — 폴백 기상")
                break
        else:
            fails = 0
            log("수면 상태", detected_sleep_hours=round(hours, 2),
                target=TARGET_SLEEP_HOURS)
            # 안전장치 1: 목표 도달
            if hours >= TARGET_SLEEP_HOURS:
                fire_alarm(f"목표 수면 {TARGET_SLEEP_HOURS}h 충족 (감지 {hours:.2f}h) — 기상!")
                break

        # 다음 폴링까지 대기 (단, 상한을 넘지 않게)
        sleep_s = POLL_INTERVAL_MIN * 60
        remaining = (cap - datetime.now()).total_seconds()
        time.sleep(max(5, min(sleep_s, remaining)))

    log("세션 종료 (링 연결은 sync 종료 시 해제됨)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("사용자 중단(Ctrl+C)")
        sys.exit(130)
    except Exception as e:
        # 어떤 예외에도 최소한 알람은 울린다 (신뢰성 최우선)
        log("치명적 예외 — 폴백 알람", error=str(e))
        fire_alarm("WakeReady 내부 오류 — 폴백 기상")
        sys.exit(1)
