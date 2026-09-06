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
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

_LOG_TAIL = deque(maxlen=6)   # TUI 카드에 보여줄 최근 로그


def emit(msg):
    """진행 로그: 최근로그 버퍼에 쌓고, 비TUI면 터미널에도 출력."""
    _LOG_TAIL.append(f"{datetime.now():%H:%M:%S} {msg}")
    if not TUI:
        print("           " + msg, flush=True)

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
TUI = _flag("--tui")
SIMULATE_HOURS = _opt("--simulate", float, None)
_poll_override = _opt("--poll", float, None)
if _poll_override is not None:
    POLL_INTERVAL_MIN = _poll_override / 60.0


def log(msg, **fields):
    ts = datetime.now().isoformat(timespec="seconds")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"ts": ts, "msg": msg, **fields}
    with open(LOG_DIR / "wakeready.jsonl", "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    _LOG_TAIL.append(f"{ts[11:19]} {msg}")   # TUI 카드용 최근로그
    if TUI:   # TUI 모드에선 카드만 그리고, 로그는 파일에만
        return
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


ATTEMPT_GAP_SEC = float(os.environ.get("ATTEMPT_GAP_SEC", "15"))  # 창 내 재시도 간격


def single_attempt(do_analyze=True):
    """sleep-analyze(옵션) + sync 1회. 성공 시 True, 실패 시 (False, 에러문자열)."""
    if do_analyze:
        try:
            run_oura("sleep-analyze", "--force")
        except Exception:
            pass
        time.sleep(BLE_GAP_SEC)  # 재광고 대기(핵심: 없으면 sync가 링을 못 찾음)
    try:
        r = run_oura("sync")
        if r.returncode == 0:
            return True, ""
        return False, (r.stderr or "")[-160:]
    except Exception as e:
        return False, str(e)


def poll_window(deadline):
    """deadline 까지 sync 성공할 때까지 계속 시도. 성공하면 hours, 끝까지 실패면 None.

    한 번 시도 성공률이 낮아도(BLE 광고 간헐성), 창 안에서 반복하면 누적 성공률이
    1에 수렴한다. 성공하면 즉시 반환하고 남은 시간은 호출측에서 쉰다(배터리 절약)."""
    emit("🔗 링 연결·동기화 시도 중...")
    t0 = time.time()
    n = 0
    while datetime.now() < deadline:
        n += 1
        # 첫 시도와 이후 4회마다 sleep-analyze(수면 재분석 갱신), 나머지는 sync만(빠름)
        ok, err = single_attempt(do_analyze=(n == 1 or n % 4 == 0))
        if ok:
            emit(f"⟳ 동기화 완료 ({time.time()-t0:.0f}초, {n}회째)")
            return latest_sleep_hours()
        rem = (deadline - datetime.now()).total_seconds()
        if rem <= 0:
            break
        emit(f"↻ {n}회 실패 — {ATTEMPT_GAP_SEC:.0f}초 후 재시도")
        time.sleep(min(ATTEMPT_GAP_SEC, rem))
    log("폴링 창 내 모든 시도 실패", attempts=n)
    return None


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


STALE_AFTER_HOURS = float(os.environ.get("STALE_AFTER_HOURS", "3"))
SYNC_REQ = LOG_DIR / "sync_request"   # 웹 '지금 동기화' 버튼이 만드는 요청 플래그


def wait_or_sync(seconds):
    """seconds 동안 대기하되, 웹에서 수동 동기화 요청이 오면 즉시 반환(True)."""
    end = time.time() + seconds
    while time.time() < end:
        if SYNC_REQ.exists():
            try:
                SYNC_REQ.unlink()
            except Exception:
                pass
            log("🔄 웹 수동 동기화 요청 — 즉시 폴링")
            return True
        time.sleep(min(2, max(0.1, end - time.time())))
    return False


def sleep_end_gap_hours(bp):
    """이 수면의 '종료 시점'이 지금(링 최신 이벤트)으로부터 몇 시간 전인지.
    링 내부시계(deciseconds) 기준. 오늘 자는 중이면 ~0, 지난밤이면 수 시간+."""
    if not bp:
        return None
    import sqlite3
    try:
        con = sqlite3.connect(DB)
        row = con.execute("SELECT MAX(ring_timestamp) FROM events").fetchone()
        con.close()
        if not row or row[0] is None:
            return None
        gap = (row[0] - bp.get("bedtime_end_ds", 0)) / 10.0 / 3600
        return gap
    except Exception:
        return None


def is_stale(bp):
    """수면 종료가 STALE_AFTER_HOURS 이상 지났으면 지난 수면(현재 세션 아님)."""
    gap = sleep_end_gap_hours(bp)
    return gap is not None and gap > STALE_AFTER_HOURS


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


def publish_status(*, phase, hours=None, est=None, status="", cap=None, next_poll=None,
                   fails=0):
    """웹뷰용 현재 상태를 logs/status.json 에 기록 (웹서버가 읽음). 항상 호출."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        rec = {
            "updated": datetime.now().isoformat(timespec="seconds"),
            "mode": "healthy" if HEALTHY_MODE else "total",
            "phase": phase, "status": status, "fails": fails,
            "hours": hours, "target_hours": TARGET_SLEEP_HOURS,
            "rem_min_target": REM_MIN_MIN, "deep_min_target": DEEP_MIN_MIN,
            "estimate": est,
            "cap_time": cap.strftime("%H:%M") if cap else CAP_TIME,
            "next_poll": next_poll.strftime("%H:%M:%S") if next_poll else None,
        }
        (LOG_DIR / "status.json").write_text(json.dumps(rec, ensure_ascii=False))
    except Exception:
        pass


def show(**kw):
    """웹용 status.json 기록(항상) + TUI 카드(TUI일 때)."""
    publish_status(**kw)
    if TUI:
        render_tui(**{k: v for k, v in kw.items() if k != "fails"})


def render_tui(*, phase, hours=None, est=None, status="", cap=None, next_poll=None):
    """ANSI 기반 라이브 카드(의존성 0). 화면을 지우고 제자리에 다시 그린다."""
    W = 46
    def rule(mid=""):
        if not mid:
            return "─" * W
        pad = W - 2 - len(mid)
        return "─" * 3 + f" {mid} " + "─" * max(0, pad - 3)
    lines = []
    mode = "건강모드" if HEALTHY_MODE else f"총{TARGET_SLEEP_HOURS:.0f}h"
    lines.append(rule(f"WakeReady · {mode}" + (" · DRY" if DRY_RUN else "")))
    if phase == "syncing":
        lines.append(" 🔗 링 연결·동기화 중...")
    elif hours is not None:
        lines.append(f" 💤 수면   {hours:4.1f}h  [{_bar(hours, TARGET_SLEEP_HOURS)}] / {TARGET_SLEEP_HOURS:.0f}h")
        if est:
            lines.append(f" 🧠 품질   {quality_label(est)}")
            lines.append(f" 📊 단계   REM {est['rem_min']}분({est['rem_pct']}%) · "
                         f"깊은 {est['deep_min']}분({est['deep_pct']}%) · 깬 {est['awake_min']}분")
    if status:
        lines.append(f" 🎯 상태   {status}")
    foot = []
    if cap is not None:
        foot.append(f"상한 {cap.strftime('%H:%M')}")
    if next_poll is not None:
        foot.append(f"다음 {next_poll.strftime('%H:%M:%S')}")
    foot.append(f"now {datetime.now().strftime('%H:%M:%S')}")
    lines.append(" ⏰ " + "  ·  ".join(foot))
    lines.append(rule())
    # 최근 로그 (QR 위에 상시 표시 → 별도 터미널 없이 진행상황 확인)
    if _LOG_TAIL:
        lines.append(" 📜 최근 로그")
        for ln in list(_LOG_TAIL):
            lines.append("   " + ln)
        lines.append(rule())
    # 웹뷰 QR을 카드 안에 포함 → 매 갱신마다 함께 그려져 계속 보임
    url, qr = web_url_and_qr()
    if url:
        lines.append(f" 🌐 {url}")
        if qr:
            lines.append(" 📷 폰으로 스캔 →")
            lines.append(qr)
    sys.stdout.write("\033[2J\033[H")           # clear + home
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def do_poll(deadline):
    """deadline 까지 폴링(성공할 때까지 반복) + 상태 출력. (hours, est, bp) 반환."""
    if SIMULATE_HOURS is not None:
        hours, bp = SIMULATE_HOURS, None
    else:
        hours = poll_window(deadline)
        bp = latest_bedtime_period() if hours is not None else None
    if hours is None:
        return None, None, None
    est = estimate_stages() if HEALTHY_MODE else None
    # 헤드라인/판정의 '총 수면'은 창(bedtime_period)이 아니라 실제 잔 시간(깬시간 제외).
    # 오피셜 '총 수면'과 같은 정의. est 있으면 그 값 사용.
    if est and est.get("total_sleep_hours"):
        hours = est["total_sleep_hours"]

    # 터미널 실시간 요약(누적 수면 + 품질) — TUI 모드에선 카드가 대신함
    remain = max(0.0, TARGET_SLEEP_HOURS - hours)
    if not TUI:
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


_WEB_CACHE = None


def web_url_and_qr():
    """(url, qr문자열) 1회 계산 후 캐시. host 없으면 (None, None)."""
    global _WEB_CACHE
    if _WEB_CACHE is not None:
        return _WEB_CACHE
    port = os.environ.get("WEB_PORT", "8777")
    host = None
    try:
        host = subprocess.run(["scutil", "--get", "LocalHostName"],
                              capture_output=True, text=True, timeout=3).stdout.strip() or None
    except Exception:
        pass
    url = f"http://{host}.local:{port}" if host else None
    qr = None
    if url:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from _qr import qr_ascii
            qr = qr_ascii(url)
        except Exception:
            pass
    _WEB_CACHE = (url, qr)
    return _WEB_CACHE


def web_banner():
    """비TUI 모드: 웹뷰 주소 + QR을 세션 시작 시 1회 출력(스크롤에 남음)."""
    url, qr = web_url_and_qr()
    if not url:
        return
    print("─" * 46)
    print(f"  🌐 웹뷰(같은 와이파이): {url}")
    if qr:
        print("  📷 폰 카메라로 스캔 →")
        print(qr)
    print("─" * 46)


def main():
    cap = cap_datetime()
    mode = "건강모드" if HEALTHY_MODE else f"총{TARGET_SLEEP_HOURS}h"
    log(f"세션 시작 [{mode}]"
        + (" [DRY-RUN]" if DRY_RUN else ""),
        target_hours=TARGET_SLEEP_HOURS, poll_min=round(POLL_INTERVAL_MIN, 2),
        cap_time=cap.isoformat(timespec="minutes"),
        rem_min=REM_MIN_MIN if HEALTHY_MODE else None,
        deep_min=DEEP_MIN_MIN if HEALTHY_MODE else None, db=DB)
    if not (TEST_ONCE or TEST_ALARM) and not TUI:
        web_banner()   # TUI는 카드 안에 QR을 상시 포함하므로 별도 배너 불필요

    # --- 테스트 모드들 ---
    if TEST_ALARM:
        log("[--test-alarm] 즉시 알람 테스트")
        fire_alarm("테스트 알람 (--test-alarm)")
        return
    if TEST_ONCE:
        log("[--once] 폴링 점검 (최대 2분간 성공할 때까지 시도)")
        hours, est, bp = do_poll(datetime.now() + timedelta(minutes=2))
        if hours is None:
            log("폴링 실패 — 링 연결/인증 확인 필요 (아이폰 BT off, 링 착용/충전)")
        else:
            gap = sleep_end_gap_hours(bp)
            if is_stale(bp):
                print(f"           ⏸️ 지난 수면 데이터입니다 (종료 {gap:.1f}시간 전). "
                      f"오늘 자면 새 수면으로 갱신돼요. → 지금은 판정 대상 아님.", flush=True)
                log("판정 결과: 지난 수면(무시 대상)", stale=True,
                    end_gap_hours=round(gap, 1) if gap is not None else None)
            else:
                fire, reason = check_wake(hours, est)
                log(f"판정 결과: {'🔔 기상조건 충족' if fire else '⏳ 아직 대기'}",
                    would_fire=fire, reason=reason,
                    end_gap_hours=round(gap, 1) if gap is not None else None)
        return

    # 지난밤 데이터 가드: 수면 '종료'가 지금으로부터 STALE_AFTER_HOURS 이상 지났으면
    # 지난 수면으로 보고 알람 미발동. 오늘 자면 종료시점이 ~지금이 되어 판정 대상이 됨.
    fails = 0
    last_hours = None   # 마지막 성공 폴링 기록 (연결 실패 시에도 카드에 계속 표시)
    last_est = None
    # 재시작해도 직전 세션의 마지막 데이터를 이어서 표시 (status.json 복원)
    try:
        prev = json.loads((LOG_DIR / "status.json").read_text())
        if prev.get("hours") is not None:
            last_hours, last_est = prev.get("hours"), prev.get("estimate")
            log("이전 상태 복원 — 새 데이터 전까지 마지막 기록 표시",
                restored_hours=last_hours)
    except Exception:
        pass
    try:                # 세션 시작 시 오래된 수동요청 플래그 제거
        SYNC_REQ.unlink()
    except Exception:
        pass
    while True:
        now = datetime.now()
        to_cap = cap - now
        if VERBOSE:
            log(f"폴링 시작 (상한까지 {int(to_cap.total_seconds()//60)}분)")

        # 안전장치 2: 상한 시각 도달
        if now >= cap:
            fire_alarm(f"안전 상한 시각({CAP_TIME}) 도달 — 무조건 기상")
            break

        # 이번 폴링 창: 성공할 때까지 이 시간까지 계속 재시도 (상한은 넘지 않게)
        window_deadline = min(now + timedelta(minutes=POLL_INTERVAL_MIN), cap)
        # 동기화 중에도 마지막 성공 기록은 유지해서 보여줌
        show(phase="syncing" if last_hours is None else "result",
             hours=last_hours, est=last_est,
             status="🔗 동기화 시도 중 (성공까지 반복)...", cap=cap, fails=fails)
        hours, est, bp = do_poll(window_deadline)
        status = ""
        if hours is None:
            fails += 1
            log("폴링 실패", consecutive_fails=fails)
            status = f"⚠️ 링 연결 실패 ({fails}회) · 마지막 기록 유지"
            if fails >= MAX_FAILS_BEFORE_FALLBACK and to_cap <= timedelta(minutes=30):
                fire_alarm("연결 실패 누적 + 상한 임박 — 폴백 기상")
                break
        else:
            fails = 0
            if is_stale(bp):
                gap = sleep_end_gap_hours(bp)
                log(f"⏸️ 지난 수면 무시 중 (종료 {gap:.1f}시간 전) — 오늘 새 수면 대기")
                status = f"⏸️ 지난 수면 (종료 {gap:.1f}h 전) — 오늘 수면 대기"
            else:
                last_hours, last_est = hours, est   # 마지막 성공 기록 저장
                fire, reason = check_wake(hours, est)
                status = "🔔 기상!" if fire else "⏳ 목표까지 대기 중"
                if fire:
                    show(phase="result", hours=hours, est=est, status=status, cap=cap)
                    fire_alarm(reason)
                    if not DRY_RUN:
                        break

        # 이번 창에서 남은 시간만 쉰다 (성공했으면 남은 ~폴링간격을 쉼 = 배터리 절약,
        # 실패로 창을 다 썼으면 바로 다음 창으로). 상한은 넘지 않게.
        remaining = (min(window_deadline, cap) - datetime.now()).total_seconds()
        wait = max(1, remaining)
        nxt = datetime.now() + timedelta(seconds=wait)
        # 표시용: 이번 폴링 성공 데이터(지난수면이어도 수치 노출, 상태 라벨로 구분),
        # 폴링 실패면 마지막 성공 기록 유지
        disp_h = hours if hours is not None else last_hours
        disp_e = est if hours is not None else last_est
        show(phase="result", hours=disp_h, est=disp_e, status=status,
             cap=cap, next_poll=nxt, fails=fails)
        if not TUI:
            log(f"다음 폴링 {nxt.strftime('%H:%M:%S')} (약 {int(wait//60)}분 후) · 웹 '지금 동기화'로 앞당기기 가능")
        wait_or_sync(wait)   # 웹 수동 동기화 요청 오면 즉시 다음 폴링

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
