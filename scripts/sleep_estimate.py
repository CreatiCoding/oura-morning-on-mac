#!/usr/bin/env python3
"""원시 링 신호(HR·RMSSD 5분 에폭·모션)로 수면단계를 '적당히' 추정.

SleepNet 모델 없이, HR/HRV/모션의 생리 패턴으로 WAKE/LIGHT/DEEP/REM 을 근사한다.
휴리스틱 근거:
  - DEEP(깊은수면): HR 최저 + RMSSD(HRV) 높음 + 무움직임. 초저녁에 몰림.
  - REM: HR가 깊은수면보다 상승(각성에 가까움) + RMSSD 상대적으로 낮고 변동 + 근무력(움직임 적음). 새벽/기상 직전에 몰림.
  - LIGHT: 그 사이. WAKE: 움직임/HR 높음.
건강 기준(성인 8h): REM 20~25%(90~120분), DEEP 15~25%(70~120분), LIGHT 50~60%.

⚠️ 이건 근사치다. REM/LIGHT 구분은 원래 신경망이 하는 어려운 부분이라 오차가 있다.
   실제 값과의 보정은 aardvark: 아침에 공식 Oura 수면수치와 며칠 대조해 임계값을 조정할 것.
사용: python sleep_estimate.py [DB]  → JSON(추정 분/비율) 출력
"""
import json
import sqlite3
import sys
import statistics as st
from pathlib import Path

DB = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent / "data" / "oura.db")
EPOCH_MIN = 5  # hrv_event 샘플 간격(분)


def bedtime_window(db):
    """가장 최근 bedtime_period 의 (start_ds, end_ds). 없으면 None."""
    try:
        con = sqlite3.connect(db)
        row = con.execute(
            "SELECT decoded_json FROM events WHERE name='bedtime_period' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        con.close()
        if not row or not row[0]:
            return None
        v = json.loads(row[0])
        return int(v["bedtime_start_ds"]), int(v["bedtime_end_ds"])
    except Exception:
        return None


def load_epochs(db, start_ds=None, end_ds=None):
    """hrv_event 를 시간순 (hr, rmssd) 에폭으로. 0값은 결측.

    ⚠️ 반드시 '오늘 밤 수면창(bedtime_period)' 안으로 스코프한다. 안 그러면 DB에 누적된
    지난 날/주간 이벤트까지 세어 REM·깊은수면이 몇 배로 부풀려진다. (window 미지정 시 자동 감지)
    """
    if start_ds is None or end_ds is None:
        win = bedtime_window(db)
        if win:
            start_ds, end_ds = win
    con = sqlite3.connect(db)
    if start_ds is not None and end_ds is not None:
        rows = con.execute(
            "SELECT ring_timestamp, decoded_json FROM events "
            "WHERE name='hrv_event' AND ring_timestamp BETWEEN ? AND ? "
            "ORDER BY ring_timestamp", (start_ds, end_ds)
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT ring_timestamp, decoded_json FROM events WHERE name='hrv_event' "
            "ORDER BY ring_timestamp").fetchall()
    con.close()
    epochs = []
    for ts, js in rows:
        v = json.loads(js)
        hrs = v.get("hr_bpm", [])
        rms = v.get("rmssd_ms", [])
        for i in range(max(len(hrs), len(rms))):
            hr = hrs[i] if i < len(hrs) else 0
            rm = rms[i] if i < len(rms) else 0
            epochs.append({"hr": hr, "rmssd": rm})
    return epochs


def classify(epochs):
    valid_hr = [e["hr"] for e in epochs if e["hr"] > 0]
    valid_rm = [e["rmssd"] for e in epochs if e["rmssd"] > 0]
    if len(valid_hr) < 5:
        return None
    hr_sorted = sorted(valid_hr)
    hr_lo = hr_sorted[int(len(hr_sorted) * 0.15)]   # 깊은수면 기준(저 HR)
    hr_hi = hr_sorted[int(len(hr_sorted) * 0.85)]
    hr_rng = max(1, hr_hi - hr_lo)
    rm_med = st.median(valid_rm) if valid_rm else 0

    n = len(epochs)
    stages = []
    for idx, e in enumerate(epochs):
        hr, rm = e["hr"], e["rmssd"]
        frac = idx / max(1, n - 1)  # 0=초저녁 ~ 1=새벽
        if hr <= 0:
            stages.append("WAKE"); continue
        # 각성: HR가 상위(각성역치) 이상
        if hr >= hr_lo + 0.8 * hr_rng:
            stages.append("WAKE"); continue
        # 깊은수면: 저 HR + 높은 RMSSD (초저녁 가중)
        deep_hr = hr <= hr_lo + 0.30 * hr_rng
        deep_hrv = rm >= rm_med
        if deep_hr and deep_hrv and frac < 0.75:
            stages.append("DEEP"); continue
        # REM: 중간~상승 HR + 상대적으로 낮은 RMSSD + 새벽 가중
        rem_hr = hr >= hr_lo + 0.35 * hr_rng
        rem_hrv = rm < rm_med
        rem_time = frac > 0.35  # 첫 사이클엔 REM 거의 없음
        if rem_hr and (rem_hrv or frac > 0.6) and rem_time:
            stages.append("REM"); continue
        stages.append("LIGHT")
    return stages


def summarize(stages):
    total_epochs = sum(1 for s in stages if s != "WAKE")
    counts = {k: stages.count(k) for k in ("DEEP", "LIGHT", "REM", "WAKE")}
    mins = {k: v * EPOCH_MIN for k, v in counts.items()}
    asleep_min = mins["DEEP"] + mins["LIGHT"] + mins["REM"]
    pct = {k: round(100 * mins[k] / asleep_min, 1) if asleep_min else 0
           for k in ("DEEP", "LIGHT", "REM")}
    return {
        "total_sleep_hours": round(asleep_min / 60, 2),
        "rem_min": mins["REM"], "deep_min": mins["DEEP"],
        "light_min": mins["LIGHT"], "awake_min": mins["WAKE"],
        "rem_pct": pct["REM"], "deep_pct": pct["DEEP"], "light_pct": pct["LIGHT"],
    }


def main():
    epochs = load_epochs(DB)
    stages = classify(epochs)
    if not stages:
        print(json.dumps({"error": "insufficient hrv_event data"})); return
    out = summarize(stages)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
