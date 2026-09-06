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
    ds_per_epoch = EPOCH_MIN * 60 * 10   # 5분 = 3000 deciseconds
    epochs = []
    for ts, js in rows:
        v = json.loads(js)
        hrs = v.get("hr_bpm", [])
        rms = v.get("rmssd_ms", [])
        n = max(len(hrs), len(rms))
        for i in range(n):
            hr = hrs[i] if i < len(hrs) else 0
            rm = rms[i] if i < len(rms) else 0
            # 각 샘플에 근사 타임스탬프(마지막 샘플=envelope ts 기준 역산)
            ets = ts - (n - 1 - i) * ds_per_epoch
            epochs.append({"hr": hr, "rmssd": rm, "ts": ets, "motion": 0.0})
    _attach_motion(db, epochs, start_ds, end_ds)
    return epochs


def _attach_motion(db, epochs, start_ds, end_ds):
    """sleep_acm_period(모션 MAD)를 에폭 타임스탬프에 근접 매칭해 붙인다."""
    if not epochs:
        return
    try:
        con = sqlite3.connect(db)
        q = ("SELECT ring_timestamp, decoded_json FROM events WHERE name='sleep_acm_period'")
        args = ()
        if start_ds is not None and end_ds is not None:
            q += " AND ring_timestamp BETWEEN ? AND ?"; args = (start_ds, end_ds)
        rows = con.execute(q, args).fetchall()
        con.close()
    except Exception:
        return
    mo = []  # (ts, motion_magnitude)
    for ts, js in rows:
        try:
            mad = json.loads(js).get("acm_mad", [])
            if mad:
                mo.append((ts, sum(mad) / len(mad)))
        except Exception:
            pass
    if not mo:
        return
    mo.sort()
    half = EPOCH_MIN * 60 * 10 / 2
    j = 0
    for e in epochs:
        # 에폭 ts 근처(±2.5분) 모션들의 평균
        vals = [m for (t, m) in mo if abs(t - e["ts"]) <= half]
        if vals:
            e["motion"] = sum(vals) / len(vals)


def _smooth(stages, min_run=2):
    """고립된 짧은 구간(1에폭 flip)을 이웃 단계로 흡수 — 생리적으로 단계는 연속적."""
    if len(stages) < 3:
        return stages
    out = stages[:]
    for i in range(1, len(out) - 1):
        if out[i] != out[i - 1] and out[i - 1] == out[i + 1]:
            out[i] = out[i - 1]   # 양옆이 같으면 가운데 1개는 오분류로 보고 흡수
    return out


MODEL_PKL = Path(__file__).resolve().parent.parent / "models" / "sleep_clf.pkl"
FEATURES = ["hr", "rmssd", "motion", "frac", "hr_z", "rm_z"]


def epoch_features(epochs):
    """에폭 → 피처 dict 리스트 (학습/추론 공용)."""
    hrs = [e["hr"] for e in epochs if e["hr"] > 0]
    rms = [e["rmssd"] for e in epochs if e["rmssd"] > 0]
    hr_m = st.mean(hrs) if hrs else 0
    hr_sd = (st.pstdev(hrs) or 1) if len(hrs) > 1 else 1
    rm_m = st.mean(rms) if rms else 0
    rm_sd = (st.pstdev(rms) or 1) if len(rms) > 1 else 1
    n = len(epochs)
    return [{
        "hr": e["hr"], "rmssd": e["rmssd"], "motion": e.get("motion", 0),
        "frac": i / max(1, n - 1),
        "hr_z": (e["hr"] - hr_m) / hr_sd, "rm_z": (e["rmssd"] - rm_m) / rm_sd,
    } for i, e in enumerate(epochs)]


def classify_model(epochs):
    """학습된 개인화 모델(models/sleep_clf.pkl)이 있으면 그것으로 예측. 없으면 None."""
    if not MODEL_PKL.exists():
        return None
    try:
        import pickle
        with open(MODEL_PKL, "rb") as f:
            bundle = pickle.load(f)
        clf, feats = bundle["clf"], bundle["features"]
        rows = epoch_features(epochs)
        X = [[r[k] for k in feats] for r in rows]
        return _smooth(list(clf.predict(X)))
    except Exception:
        return None


def classify(epochs):
    # 개인화 모델 우선, 없으면 휴리스틱
    m = classify_model(epochs)
    if m is not None:
        return m
    valid_hr = [e["hr"] for e in epochs if e["hr"] > 0]
    valid_rm = [e["rmssd"] for e in epochs if e["rmssd"] > 0]
    if len(valid_hr) < 5:
        return None

    def pct(sorted_vals, p):
        return sorted_vals[min(len(sorted_vals) - 1, int(len(sorted_vals) * p))]
    hs, rs = sorted(valid_hr), sorted(valid_rm)
    hr_t33, hr_t66 = pct(hs, 0.33), pct(hs, 0.66)   # HR 하위/상위 터셀
    rm_t50, rm_t66 = pct(rs, 0.50), pct(rs, 0.66)   # HRV 중앙/상위
    motions = [e.get("motion", 0) for e in epochs if e.get("motion", 0) > 0]
    mo_hi = sorted(motions)[int(len(motions) * 0.80)] if len(motions) >= 5 else None
    n = len(epochs)

    # 단기 HR 변동성(±2 에폭 표준편차): REM은 HR/호흡이 불규칙 → 변동성 높음
    hr_series = [e["hr"] for e in epochs]
    def local_var(i):
        w = [hr_series[j] for j in range(max(0, i - 2), min(n, i + 3))
             if hr_series[j] > 0]
        return st.pstdev(w) if len(w) >= 2 else 0
    lv = [local_var(i) for i in range(n)]
    lv_hi = pct(sorted([x for x in lv if x > 0]) or [0], 0.60)

    stages = []
    for idx, e in enumerate(epochs):
        hr, rm, mo = e["hr"], e["rmssd"], e.get("motion", 0)
        frac = idx / max(1, n - 1)  # 0=초저녁 ~ 1=새벽
        still = (mo_hi is None) or (mo < mo_hi)
        if hr <= 0:
            stages.append("WAKE"); continue
        # 각성: 큰 움직임(우선) 또는 HR 매우 높음
        if mo_hi is not None:
            if mo >= mo_hi and hr >= hr_t66:
                stages.append("WAKE"); continue
        elif hr >= pct(hs, 0.90):
            stages.append("WAKE"); continue
        # 깊은수면(SWS): 최저 HR + 높은 HRV + 무움직임 + 전반부 가중.
        deep_still = (mo_hi is None) or (mo < mo_hi * 0.6)
        if hr <= hr_t33 and rm >= rm_t50 and deep_still and frac < 0.6:
            stages.append("DEEP"); continue
        # REM: 근무력(저움직임) + HR 단기변동 큼 or 후반부 + HR가 최저는 아님
        rem_var = lv[idx] >= lv_hi
        if still and hr > hr_t33 and (rem_var or frac > 0.5) and frac > 0.30:
            stages.append("REM"); continue
        stages.append("LIGHT")
    return _smooth(stages)


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
