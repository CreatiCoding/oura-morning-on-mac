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

# ---- .env 자동 로드 (직접 실행해도 설정이 적용되도록) ----
ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv():
    envf = ROOT / ".env"
    if not envf.exists():
        return
    for line in envf.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

# ---- 설정 (환경변수로 오버라이드) ----
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

# 건강 수면 모드: 총시간 + REM/깊은수면 추정치 충족 시 기상 (모델 없이 휴리스틱 추정)
# 기본 off. 켜도 안전 상한 시각은 항상 보장되어 늦잠 위험 없음.
HEALTHY_MODE = os.environ.get("HEALTHY_MODE", "0") == "1"
# 기본값은 사용자 실측(REM 79분·깊은 67분 @7.14h)에 맞춰 achievable하게 설정.
# 교과서 권장은 REM 90~120·깊은 70~120이나, 그러면 거의 미달→상한까지 대기하므로
# 며칠 로그로 본인 기준에 맞게 상향 튜닝할 것.
REM_MIN_MIN = float(os.environ.get("REM_MIN_MIN", "70"))
DEEP_MIN_MIN = float(os.environ.get("DEEP_MIN_MIN", "55"))

# ---- 테스트/디버그 플래그 (CLI) ----
#   --once         : 폴링 1회만 하고 현재 상태 출력 후 종료 (연결/판정 빠른 점검)
#   --test-alarm   : 즉시 알람만 발동하고 종료 (알람 경로 테스트)
#   --dry-run      : 정상 루프지만 조건 충족 시 실제 알람 대신 "발동했을 것" 로그
#   --simulate=H   : 수면시간을 H로 가정(링 연결 없이 판정 로직 테스트)
#   --poll=SEC     : 폴링 간격을 SEC초로 강제(빠른 테스트용)
#   --verbose      : 폴링 원본/상세 로그
_args = sys.argv[1:]
def _flag(name): return name in _args
def _opt(name, cast=str, default=None):
    pref = name + "="
    for a in _args:
        if a.startswith(pref):
            return cast(a[len(pref):])
    return default

TEST_ONCE = _flag("--once")
TEST_ALARM = _flag("--test-alarm")
DRY_RUN = _flag("--dry-run")
VERBOSE = _flag("--verbose")
SIMULATE_HOURS = _opt("--simulate", float, None)
_poll_override = _opt("--poll", float, None)
if _poll_override is not None:
    POLL_INTERVAL_MIN = _poll_override / 60.0


def log(msg, **fields):
    ts = datetime.now().isoformat(timespec="seconds")
    # 터미널엔 주요 필드도 함께 보이도록 요약
    extra = " ".join(f"{k}={v}" for k, v in fields.items()
                     if k != "estimate" and v is not None)
    line = f"[{ts}] {msg}" + (f"  ({extra})" if extra else "")
    print(line, flush=True)
    if fields.get("estimate"):
        e = fields["estimate"]
        print(f"           추정: 총{e.get('total_sleep_hours')}h "
              f"REM{e.get('rem_min')}분({e.get('rem_pct')}%) "
              f"깊은{e.get('deep_min')}분({e.get('deep_pct')}%) "
              f"얕은{e.get('light_min')}분 깬{e.get('awake_min')}분", flush=True)
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


BLE_GAP_SEC = float(os.environ.get("BLE_GAP_SEC", "12"))   # BLE 명령 사이 재광고 대기
SYNC_RETRIES = int(os.environ.get("SYNC_RETRIES", "3"))


def poll_once():
    """sleep-analyze + sync 후 최신 bedtime_period duration_hours 반환. 실패 시 None.

    주의: 링은 연속 재연결에 약하다. sleep-analyze 직후 곧바로 sync 하면 재광고 전이라
    'no matching ring' 또는 인증거부가 난다. → 명령 사이 딜레이 + sync 재시도로 완화.
    """
    print("           🔗 링 연결·수면분석·동기화 중... (30초~2분 소요)", flush=True)
    t0 = time.time()
    try:
        run_oura("sleep-analyze", "--force")  # 링이 분석을 돌리도록 요청(best-effort)
    except Exception as e:
        log("sleep-analyze 실패(무시)", error=str(e))
    # 링이 재광고할 시간을 준다 (핵심: 이게 없으면 sync가 링을 못 찾음)
    time.sleep(BLE_GAP_SEC)
    for attempt in range(1, SYNC_RETRIES + 1):
        try:
            r = run_oura("sync")
            if r.returncode == 0:
                print(f"           ⟳ 동기화 완료 ({time.time()-t0:.0f}초)", flush=True)
                break
            err = (r.stderr or "")[-160:]
        except Exception as e:
            err = str(e)
        if attempt < SYNC_RETRIES:
            print(f"           ↻ sync 재시도 {attempt}/{SYNC_RETRIES-1} "
                  f"({BLE_GAP_SEC:.0f}초 후)...", flush=True)
            time.sleep(BLE_GAP_SEC)
        else:
            log("sync 실패 (링 연결 확인: 아이폰 BT off, 링 착용, 맥 근처)", stderr=err)
            return None
    return latest_sleep_hours()


def estimate_stages():
    """sleep_estimate 로 REM/깊은수면 추정. 실패 시 None."""
    try:
        import importlib.util
        p = Path(__file__).resolve().parent / "sleep_estimate.py"
        spec = importlib.util.spec_from_file_location("sleep_estimate", p)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        epochs = m.load_epochs(DB)
        stages = m.classify(epochs)
        return m.summarize(stages) if stages else None
    except Exception as e:
        log("수면단계 추정 실패", error=str(e))
        return None


def latest_bedtime_period():
    """DB의 가장 최근 bedtime_period 를 dict 로 반환. {bedtime_start_ds, bedtime_end_ds, duration_hours}"""
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
        return json.loads(row[0])
    except Exception as e:
        log("DB 읽기 실패", error=str(e))
        return None


def latest_sleep_hours():
    bp = latest_bedtime_period()
    return float(bp["duration_hours"]) if bp else None


def fire_alarm(reason):
    if DRY_RUN:
        log("🔔 [DRY-RUN] 알람을 발동했을 것", reason=reason)
        return
    log("🔔 ALARM 발동", reason=reason)
    try:
        subprocess.run(["bash", ALARM_SH, reason], timeout=300)
    except Exception as e:
        # 알람 스크립트마저 실패하면 최후의 수단
        log("alarm.sh 실패 — say 폴백", error=str(e))
        os.system('say "Wake up. WakeReady fallback alarm." 2>/dev/null')


def check_wake(hours, est):
    """기상 조건 충족 여부와 사유 문자열. (fire여부, reason)"""
    if HEALTHY_MODE:
        if est and hours >= TARGET_SLEEP_HOURS \
                and est["rem_min"] >= REM_MIN_MIN \
                and est["deep_min"] >= DEEP_MIN_MIN:
            return True, (f"건강 수면 충족 — 총 {hours:.2f}h, REM {est['rem_min']}분, "
                          f"깊은 {est['deep_min']}분 (추정) — 기상!")
    elif hours >= TARGET_SLEEP_HOURS:
        return True, f"목표 수면 {TARGET_SLEEP_HOURS}h 충족 (감지 {hours:.2f}h) — 기상!"
    return False, None


def _bar(cur, target, width=20):
    frac = max(0.0, min(1.0, cur / target)) if target else 0
    n = int(frac * width)
    return "█" * n + "░" * (width - n)


def quality_label(est):
    """추정치로 '얼마나 잘 잤나' 한 줄 평가."""
    if not est:
        return "?"
    asleep = est["rem_min"] + est["deep_min"] + est["light_min"]
    eff = 100 * asleep / (asleep + est["awake_min"]) if (asleep + est["awake_min"]) else 0
    good = est["rem_pct"] >= 18 and est["deep_pct"] >= 13 and eff >= 85
    ok = est["rem_pct"] >= 13 and est["deep_pct"] >= 10 and eff >= 78
    tag = "😴 잘 자는 중" if good else ("🙂 양호" if ok else "😐 뒤척임 많음")
    return f"{tag} (효율 {eff:.0f}%)"


def do_poll():
    """한 번 폴링 + 친근한 실시간 상태 출력. (hours, est, bp) 반환. bp=최신 bedtime_period dict."""
    if SIMULATE_HOURS is not None:
        hours, bp = SIMULATE_HOURS, None
    else:
        hours = poll_once()
        bp = latest_bedtime_period() if hours is not None else None
    if hours is None:
        return None, None, None
    est = estimate_stages() if HEALTHY_MODE else None

    # 터미널 실시간 요약(누적 수면 + 품질)
    remain = max(0.0, TARGET_SLEEP_HOURS - hours)
    print(f"           💤 지금까지 {hours:.1f}h  [{_bar(hours, TARGET_SLEEP_HOURS)}] "
          f"목표 {TARGET_SLEEP_HOURS:.0f}h까지 {remain:.1f}h", flush=True)
    if est:
        print(f"           🧠 {quality_label(est)} | REM {est['rem_min']}분({est['rem_pct']}%) · "
              f"깊은 {est['deep_min']}분({est['deep_pct']}%) · 깬 {est['awake_min']}분", flush=True)

    # 미충족 사유
    if HEALTHY_MODE and est:
        need = []
        if hours < TARGET_SLEEP_HOURS: need.append(f"총{hours:.1f}/{TARGET_SLEEP_HOURS:.0f}h")
        if est["rem_min"] < REM_MIN_MIN: need.append(f"REM{est['rem_min']}/{REM_MIN_MIN:.0f}분")
        if est["deep_min"] < DEEP_MIN_MIN: need.append(f"깊은{est['deep_min']}/{DEEP_MIN_MIN:.0f}분")
        status = "✅ 충족!" if not need else "⏳ 부족: " + ", ".join(need)
        log(f"수면 판정 — {status}", detected_sleep_hours=round(hours, 2), estimate=est)
    else:
        log("수면 상태", detected_sleep_hours=round(hours, 2), target=TARGET_SLEEP_HOURS)
    return hours, est, bp


def main():
    cap = cap_datetime()
    mode = "건강모드" if HEALTHY_MODE else f"총{TARGET_SLEEP_HOURS}h"
    log(f"세션 시작 [{mode}]"
        + (" [DRY-RUN]" if DRY_RUN else ""),
        target_hours=TARGET_SLEEP_HOURS, poll_min=round(POLL_INTERVAL_MIN, 2),
        cap_time=cap.isoformat(timespec="minutes"),
        rem_min=REM_MIN_MIN if HEALTHY_MODE else None,
        deep_min=DEEP_MIN_MIN if HEALTHY_MODE else None, db=DB)

    # --- 테스트 모드들 ---
    if TEST_ALARM:
        log("[--test-alarm] 즉시 알람 테스트")
        fire_alarm("테스트 알람 (--test-alarm)")
        return
    if TEST_ONCE:
        log("[--once] 1회 폴링 점검")
        hours, est, bp = do_poll()
        if hours is None:
            log("폴링 실패 — 링 연결/인증 확인 필요 (아이폰 BT off, 링 착용/충전)")
        else:
            fire, reason = check_wake(hours, est)
            log(f"판정 결과: {'🔔 기상조건 충족' if fire else '⏳ 아직 대기'}",
                would_fire=fire, reason=reason)
        return

    # 지난밤 데이터 가드: 세션 시작 시 이미 완료된(≥목표) 수면기록의 start_ds 를 stale 로 표시.
    # 그 기록과 동일 세션(start_ds 같음)엔 절대 알람 발동하지 않음 → 오늘 새 수면만 판정.
    stale_start = None
    baseline_set = False

    fails = 0
    while True:
        now = datetime.now()
        to_cap = cap - now
        if VERBOSE:
            log(f"폴링 시작 (상한까지 {int(to_cap.total_seconds()//60)}분)")

        # 안전장치 2: 상한 시각 도달
        if now >= cap:
            fire_alarm(f"안전 상한 시각({CAP_TIME}) 도달 — 무조건 기상")
            break

        hours, est, bp = do_poll()
        if hours is None:
            fails += 1
            log("폴링 실패", consecutive_fails=fails)
            if fails >= MAX_FAILS_BEFORE_FALLBACK and to_cap <= timedelta(minutes=30):
                fire_alarm("연결 실패 누적 + 상한 임박 — 폴백 기상")
                break
        else:
            fails = 0
            # 첫 성공 폴링에서 기준선 설정: 이미 완료된 수면이면 지난밤 것으로 간주
            if not baseline_set and bp is not None:
                baseline_set = True
                if hours >= TARGET_SLEEP_HOURS:
                    stale_start = bp.get("bedtime_start_ds")
                    log("⏸️ 지난밤 데이터 감지 (이미 완료된 수면) — 오늘 새 수면 시작까지 대기",
                        stale_start=stale_start, detected=round(hours, 2))
            is_stale = (bp is not None and stale_start is not None
                        and bp.get("bedtime_start_ds") == stale_start)
            if is_stale:
                log("지난밤 기록 무시 중 (오늘 수면 시작 대기)")
            else:
                fire, reason = check_wake(hours, est)
                if fire:
                    fire_alarm(reason)
                    if not DRY_RUN:
                        break

        # 다음 폴링까지 대기 (단, 상한을 넘지 않게)
        sleep_s = POLL_INTERVAL_MIN * 60
        remaining = (cap - datetime.now()).total_seconds()
        wait = max(5, min(sleep_s, remaining))
        nxt = (datetime.now() + timedelta(seconds=wait)).strftime("%H:%M:%S")
        log(f"다음 폴링 {nxt} (약 {int(wait//60)}분 후)")
        time.sleep(wait)

    log("세션 종료 (링 연결은 sync 종료 시 해제됨)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("사용자 중단(Ctrl+C)")
        sys.exit(130)
    except Exception as e:
        # 실제 야간 세션에서만 예외 시 폴백 알람(놓치면 안 됨).
        # 테스트/확인 모드(--once/--test-alarm/--dry-run)에선 알람을 울리지 않는다.
        if TEST_ONCE or TEST_ALARM or DRY_RUN:
            log("예외 발생 (테스트 모드 — 알람 안 울림)", error=str(e))
            sys.exit(1)
        log("치명적 예외 — 폴백 알람", error=str(e))
        fire_alarm("WakeReady 내부 오류 — 폴백 기상")
        sys.exit(1)
