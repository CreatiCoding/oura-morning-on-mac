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

_pos = [a for a in sys.argv[1:] if not a.startswith("--")]
DB = _pos[0] if _pos else str(Path(__file__).resolve().parent.parent / "data" / "oura.db")
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


def ring_offset(db):
    """링 시계(deciseconds) → unix 변환 오프셋. unix = offset + ds/10.

    링이 보내는 time_sync 이벤트({"unix_time":...})로 계산. 여러 개면 가장 최근 것.
    (실측: 4일간 오프셋 편차 46초 이내 → 5분 에폭 정렬엔 충분.) 없으면 None."""
    try:
        con = sqlite3.connect(db)
        row = con.execute(
            "SELECT ring_timestamp, decoded_json FROM events WHERE name='time_sync' "
            "ORDER BY ring_timestamp DESC LIMIT 1").fetchone()
        con.close()
        if not row or not row[1]:
            return None
        return json.loads(row[1])["unix_time"] - row[0] / 10.0
    except Exception:
        return None


def night_windows(db, min_hours=1.0):
    """DB 에 기록된 '밤'들의 (start_ds, end_ds) 목록(오래된 순).

    bedtime_period 는 같은 밤에 대해 start 가 거의 같고 end 만 늘어나는 레코드가 반복된다.
    start 가 ±10분 안이면 같은 밤으로 묶고 end 는 최댓값을 쓴다."""
    try:
        con = sqlite3.connect(db)
        rows = con.execute(
            "SELECT decoded_json FROM events WHERE name='bedtime_period' ORDER BY id").fetchall()
        con.close()
    except Exception:
        return []
    nights = []   # [start, end]
    for (js,) in rows:
        try:
            v = json.loads(js); s, e = int(v["bedtime_start_ds"]), int(v["bedtime_end_ds"])
        except Exception:
            continue
        for n in nights:
            if abs(n[0] - s) <= 6000:      # 10분 = 6000 ds
                n[0] = min(n[0], s); n[1] = max(n[1], e); break
        else:
            nights.append([s, e])
    nights.sort()
    return [(s, e) for s, e in nights if (e - s) / 36000.0 >= min_hours]


def _rows(db, name, start_ds, end_ds):
    con = sqlite3.connect(db)
    rows = con.execute(
        f"SELECT ring_timestamp, decoded_json FROM events WHERE name='{name}' "
        "AND ring_timestamp BETWEEN ? AND ? ORDER BY ring_timestamp",
        (start_ds, end_ds)).fetchall()
    con.close()
    out = []
    for ts, js in rows:
        try:
            out.append((ts, json.loads(js)))
        except Exception:
            pass
    return out


def _clean_ibi(events, tol=0.2, max_diff=200):
    """IBI 아티팩트 제거. PPG 는 박동 누락(약 2배 간격)·오검출이 흔해 그대로 쓰면 RMSSD 가
    링 자체값의 5~10배로 부풀려진다(실측). 에폭 중앙값 ±tol 밖의 박동을 버리고, 인접차이는
    같은 이벤트 안의 '살아남은 연속 박동' 사이 + |차이|≤max_diff 만 센다.
    반환: (clean_beats, diffs, bad_fraction). 정리 후 값은 링 hrv_event 의 rmssd 와 근접."""
    allb = [x for ev in events for x in ev]
    if len(allb) < 10:
        return allb, [], 0.0
    med = st.median(allb)
    lo, hi = med * (1 - tol), med * (1 + tol)
    clean, diffs, nbad = [], [], 0
    for ev in events:
        prev = None
        for x in ev:
            if lo <= x <= hi:
                clean.append(x)
                if prev is not None and abs(x - prev) <= max_diff:
                    diffs.append(x - prev)
                prev = x
            else:
                nbad += 1; prev = None
    return clean, diffs, nbad / len(allb)


def build_epochs(db, start_ds, end_ds, epoch_min=EPOCH_MIN):
    """수면창을 epoch_min 격자로 잘라, 각 에폭의 심박(IBI)·HRV·움직임·체온 피처를 만든다.

    입력 이벤트(모두 링이 밤새 연속 기록):
      ibi_and_amplitude_event  ~6.6초마다 6박동(ibi_ms)  → hr, rmssd, sdnn, ibi_cv, n_beats
      sleep_acm_period         30초마다 acm_mad 6개       → motion(mean), motion_max, motion_frac
      motion_event             움직임 감지 시            → motion_sec
      temp_event               60초마다 temps_c 3개       → temp
    격자는 start_ds 에서 시작하므로 공식 API 의 5분 히프노그램(bedtime_start 기준)과
    인덱스가 1:1 로 맞는다(학습 정렬용). 데이터 없는 에폭은 hr=0(결측)."""
    step = epoch_min * 60 * 10
    n = max(0, int((end_ds - start_ds) // step))
    if n <= 0:
        return []
    ibi = [[] for _ in range(n)]
    mad = [[] for _ in range(n)]
    msec = [0.0] * n
    temp = [[] for _ in range(n)]
    def idx(ts):
        i = int((ts - start_ds) // step)
        return i if 0 <= i < n else None
    for ts, v in _rows(db, "ibi_and_amplitude_event", start_ds, end_ds):
        i = idx(ts)
        if i is not None:
            # 이벤트(연속 6박동) 단위로 보관: 인접차이는 같은 이벤트 안에서만 계산(이벤트 사이는 박동 누락)
            ibi[i].append([x for x in v.get("ibi_ms", []) if 300 <= x <= 2000])
    for ts, v in _rows(db, "sleep_acm_period", start_ds, end_ds):
        i = idx(ts)
        if i is not None:
            mad[i].extend(v.get("acm_mad", []))
    for ts, v in _rows(db, "motion_event", start_ds, end_ds):
        i = idx(ts)
        if i is not None:
            msec[i] += float(v.get("motion_seconds", 0) or 0)
    for ts, v in _rows(db, "temp_event", start_ds, end_ds):
        i = idx(ts)
        if i is not None:
            temp[i].extend(t for t in v.get("temps_c", []) if 20 < t < 42)
    epochs = []
    for i in range(n):
        raw = [x for ev in ibi[i] for x in ev]
        b, diffs, bad = _clean_ibi(ibi[i])
        if len(b) >= 10:
            mean = st.mean(b)
            hr = 60000.0 / mean
            rmssd = (sum(d * d for d in diffs) / len(diffs)) ** 0.5 if diffs else 0.0
            sdnn = st.pstdev(b) if len(b) > 1 else 0.0
            cv = sdnn / mean if mean else 0.0
        else:
            hr = rmssd = sdnn = cv = 0.0
            b = raw
        m = mad[i]
        epochs.append({
            "ts": start_ds + i * step + step,   # 에폭 끝 시각(기존 hrv_event ts 관례와 동일)
            "hr": round(hr, 1), "rmssd": round(rmssd, 1), "sdnn": round(sdnn, 1),
            "ibi_cv": round(cv, 4), "n_beats": len(b), "ibi_bad": round(bad, 3),
            "motion": (sum(m) / len(m)) if m else 0.0,
            "motion_max": max(m) if m else 0.0,
            "motion_frac": (sum(1 for x in m if x > 1.0) / len(m)) if m else 0.0,
            "motion_sec": msec[i],
            "temp": (sum(temp[i]) / len(temp[i])) if temp[i] else 0.0,
        })
    return epochs


def load_epochs(db, start_ds=None, end_ds=None):
    """오늘 밤 수면창(bedtime_period)의 5분 에폭 목록. IBI 원본이 있으면 격자 기반(build_epochs),
    없으면(구 DB) 5분 hrv_event 기반으로 폴백.

    ⚠️ 반드시 '오늘 밤 수면창' 안으로 스코프한다. 안 그러면 DB에 누적된 지난 날 이벤트까지
    세어 REM·깊은수면이 몇 배로 부풀려진다. (window 미지정 시 자동 감지)"""
    if start_ds is None or end_ds is None:
        win = bedtime_window(db)
        if win:
            start_ds, end_ds = win
    if start_ds is not None and end_ds is not None:
        ep = build_epochs(db, start_ds, end_ds)
        if sum(1 for e in ep if e["n_beats"] > 0) >= 3:
            return ep
    return _load_epochs_hrv(db, start_ds, end_ds)


def _load_epochs_hrv(db, start_ds=None, end_ds=None):
    """(폴백) hrv_event 를 시간순 (hr, rmssd) 에폭으로. 0값은 결측."""
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
            ets = ts - (n - 1 - i) * ds_per_epoch
            epochs.append({"hr": hr, "rmssd": rm, "ts": ets, "motion": 0.0, "n_beats": 0})
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
    for e in epochs:
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
# 학습/추론 공용 피처. 순서 바꾸면 기존 models/sleep_clf.pkl 과 안 맞으니 재학습 필요.
FEATURES = ["hr", "rmssd", "sdnn", "ibi_cv", "ibi_bad", "motion", "motion_max", "motion_frac", "motion_sec",
            "temp_dev", "frac", "hr_z", "rm_z", "hr_lv", "hr_ctx", "rm_ctx", "mo_ctx", "has_data"]


def _ctx(vals, i, w):
    seg = [v for v in vals[max(0, i - w):i + w + 1] if v > 0]
    return st.mean(seg) if seg else 0.0


def epoch_features(epochs):
    """에폭 → 피처 dict 리스트 (학습/추론 공용).

    밤 안에서의 z-점수(hr_z, rm_z)와 ±3에폭 문맥 평균(*_ctx), 단기 HR 변동(hr_lv)을 포함해
    개인·날짜별 절대값 차이에 덜 흔들리게 한다. has_data=0 이면 그 에폭은 결측(링 미측정)."""
    hrs = [e["hr"] for e in epochs if e["hr"] > 0]
    rms = [e["rmssd"] for e in epochs if e["rmssd"] > 0]
    tps = [e.get("temp", 0) for e in epochs if e.get("temp", 0) > 0]
    hr_m = st.mean(hrs) if hrs else 0
    hr_sd = (st.pstdev(hrs) or 1) if len(hrs) > 1 else 1
    rm_m = st.mean(rms) if rms else 0
    rm_sd = (st.pstdev(rms) or 1) if len(rms) > 1 else 1
    tp_med = st.median(tps) if tps else 0
    n = len(epochs)
    hr_series = [e["hr"] for e in epochs]
    rm_series = [e["rmssd"] for e in epochs]
    mo_series = [e.get("motion", 0) for e in epochs]
    out = []
    for i, e in enumerate(epochs):
        w = [hr_series[j] for j in range(max(0, i - 2), min(n, i + 3)) if hr_series[j] > 0]
        out.append({
            "hr": e["hr"], "rmssd": e["rmssd"],
            "sdnn": e.get("sdnn", 0), "ibi_cv": e.get("ibi_cv", 0), "ibi_bad": e.get("ibi_bad", 0),
            "motion": e.get("motion", 0), "motion_max": e.get("motion_max", 0),
            "motion_frac": e.get("motion_frac", 0), "motion_sec": e.get("motion_sec", 0),
            "temp_dev": (e.get("temp", 0) - tp_med) if e.get("temp", 0) > 0 else 0,
            "frac": i / max(1, n - 1),
            "hr_z": (e["hr"] - hr_m) / hr_sd if e["hr"] > 0 else 0,
            "rm_z": (e["rmssd"] - rm_m) / rm_sd if e["rmssd"] > 0 else 0,
            "hr_lv": st.pstdev(w) if len(w) >= 2 else 0,
            "hr_ctx": _ctx(hr_series, i, 3), "rm_ctx": _ctx(rm_series, i, 3),
            "mo_ctx": _ctx(mo_series, i, 3),
            "has_data": 1 if e["hr"] > 0 else 0,
        })
    return out


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
        pred = [str(x) for x in clf.predict(X)]
        # 결측 에폭(링 미측정)은 휴리스틱과 같이 WAKE 로 — 모델이 '없음=수면'을 배우지 않게 학습에서도 제외됨
        pred = [("WAKE" if r["has_data"] == 0 else s) for r, s in zip(rows, pred)]
        return _smooth(pred)
    except Exception:
        return None


def classify(epochs):
    return classify_with_method(epochs)[0]


def classify_with_method(epochs):
    """(stages, method). method: 'model'(개인화 모델) | 'heuristic'. 실패 시 (None, None)."""
    m = classify_model(epochs)
    if m is not None:
        return m, "model"
    h = _classify_heuristic(epochs)
    return (h, "heuristic") if h else (None, None)


def _classify_heuristic(epochs):
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


# ── 정규화 데이터 저장소 (같은 oura.db 안, wr_ 접두) ─────────────────────────
# events        : 링 원본(raw)                                  ← open_oura 가 씀
# wr_sleep_nights/wr_sleep_epochs : 정규화된 밤/5분 에폭. source='local'(맥 추정) | 'cloud'(Oura 공식)
STAGE_DIGIT = {"DEEP": "1", "LIGHT": "2", "REM": "3", "WAKE": "4"}   # Oura API sleep_phase_5_min 과 동일
DIGIT_STAGE = {v: k for k, v in STAGE_DIGIT.items()}
EPOCH_COLS = ["hr", "rmssd", "sdnn", "ibi_cv", "ibi_bad", "motion", "motion_max",
              "motion_frac", "motion_sec", "temp"]


def _connect(db):
    con = sqlite3.connect(db, timeout=10)   # oura sync 가 쓰는 중이면 최대 10초 대기
    con.executescript("""
    CREATE TABLE IF NOT EXISTS wr_sleep_nights (
      source TEXT NOT NULL, night_start_unix INTEGER NOT NULL, night_end_unix INTEGER,
      day TEXT, method TEXT,
      total_sleep_sec INTEGER, rem_sec INTEGER, deep_sec INTEGER, light_sec INTEGER, awake_sec INTEGER,
      efficiency REAL, hypnogram TEXT, raw_json TEXT, updated_unix INTEGER NOT NULL,
      PRIMARY KEY (source, night_start_unix));
    CREATE TABLE IF NOT EXISTS wr_sleep_epochs (
      source TEXT NOT NULL, night_start_unix INTEGER NOT NULL, epoch_idx INTEGER NOT NULL,
      ts_unix INTEGER, stage TEXT,
      hr REAL, rmssd REAL, sdnn REAL, ibi_cv REAL, ibi_bad REAL,
      motion REAL, motion_max REAL, motion_frac REAL, motion_sec REAL, temp REAL,
      PRIMARY KEY (source, night_start_unix, epoch_idx));
    """)
    return con


def _night_key(con, source, start_unix, tol_sec=900):
    """같은 밤인데 시작 시각이 몇 초~몇 분 흔들려도(bedtime_period 재계산) 한 행으로 모은다."""
    row = con.execute(
        "SELECT night_start_unix FROM wr_sleep_nights WHERE source=? AND ABS(night_start_unix-?)<=? "
        "ORDER BY ABS(night_start_unix-?) LIMIT 1", (source, start_unix, tol_sec, start_unix)).fetchone()
    return int(row[0]) if row else int(start_unix)


def _upsert_night(con, source, key, end_unix, day, method, mins, eff, hyp, raw, epochs_rows):
    import time as _t
    con.execute(
        "INSERT OR REPLACE INTO wr_sleep_nights VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (source, key, end_unix, day, method,
         (mins["REM"] + mins["DEEP"] + mins["LIGHT"]) * 60, mins["REM"] * 60, mins["DEEP"] * 60,
         mins["LIGHT"] * 60, mins["WAKE"] * 60, eff, hyp, raw, int(_t.time())))
    con.execute("DELETE FROM wr_sleep_epochs WHERE source=? AND night_start_unix=?", (source, key))
    con.executemany(
        "INSERT INTO wr_sleep_epochs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(source, key, i, r.get("ts_unix"), r.get("stage")) + tuple(r.get(c) for c in EPOCH_COLS)
         for i, r in enumerate(epochs_rows)])
    con.commit()


def store_local_night(db, start_ds, end_ds, epochs, stages, method):
    """맥이 추정한 오늘 밤(정규화 에폭 + 단계)을 저장. 링 시계 환산 불가(time_sync 없음)면 None."""
    off = ring_offset(db)
    if off is None or not epochs or not stages:
        return None
    from datetime import datetime as _dt
    start_unix = int(off + start_ds / 10); end_unix = int(off + end_ds / 10)
    mins = {k: EPOCH_MIN * stages.count(k) for k in STAGE_DIGIT}
    asleep = mins["REM"] + mins["DEEP"] + mins["LIGHT"]
    eff = round(100.0 * asleep / (asleep + mins["WAKE"]), 1) if asleep + mins["WAKE"] else None
    rows = [dict(e, ts_unix=int(off + e["ts"] / 10), stage=s) for e, s in zip(epochs, stages)]
    con = _connect(db)
    try:
        key = _night_key(con, "local", start_unix)
        _upsert_night(con, "local", key, end_unix, _dt.fromtimestamp(end_unix).strftime("%Y-%m-%d"),
                      method, mins, eff, "".join(STAGE_DIGIT[s] for s in stages), None, rows)
    finally:
        con.close()
    return key


def store_cloud_night(db, rec):
    """공식 API sleep 레코드 1건(가공된 정규화 데이터)을 저장. 반환: night key 또는 None."""
    from datetime import datetime as _dt
    try:
        start_unix = int(_dt.fromisoformat(rec["bedtime_start"]).timestamp())
        end_unix = int(_dt.fromisoformat(rec["bedtime_end"]).timestamp())
    except Exception:
        return None
    hyp = rec.get("sleep_phase_5_min") or ""
    mins = {k: round((rec.get(f) or 0) / 60) for k, f in
            (("REM", "rem_sleep_duration"), ("DEEP", "deep_sleep_duration"),
             ("LIGHT", "light_sleep_duration"), ("WAKE", "awake_time"))}
    def series(name):
        s = rec.get(name) or {}
        return list(s.get("items") or [])
    hr, hrv = series("heart_rate"), series("hrv")
    n = max(len(hyp), len(hr), len(hrv))
    rows = []
    for i in range(n):
        rows.append({"ts_unix": start_unix + (i + 1) * 300,
                     "stage": DIGIT_STAGE.get(hyp[i]) if i < len(hyp) else None,
                     "hr": hr[i] if i < len(hr) else None, "rmssd": hrv[i] if i < len(hrv) else None})
    con = _connect(db)
    try:
        _upsert_night(con, "cloud", start_unix, end_unix, rec.get("day"), "oura_api", mins,
                      rec.get("efficiency"), hyp, json.dumps(rec, ensure_ascii=False), rows)
    finally:
        con.close()
    return start_unix


def _night_row_to_summary(row):
    (source, start, end, day, method, tot, rem, deep, light, awake, eff, hyp) = row
    asleep = (rem + deep + light) or 0
    pct = lambda x: round(100 * x / asleep, 1) if asleep else 0
    from datetime import datetime as _dt
    return {"total_sleep_hours": round(asleep / 3600, 2),
            "rem_min": rem // 60, "deep_min": deep // 60, "light_min": light // 60, "awake_min": awake // 60,
            "rem_pct": pct(rem), "deep_pct": pct(deep), "light_pct": pct(light),
            "source": source, "method": method, "day": day, "efficiency": eff,
            "bedtime_start": _dt.fromtimestamp(start).isoformat(timespec="minutes"),
            "bedtime_end": _dt.fromtimestamp(end).isoformat(timespec="minutes") if end else None,
            "hypnogram": hyp}


def cloud_night_for(db, start_unix=None, end_unix=None, max_age_hours=24, min_hours=1.0):
    """표시용 클라우드 공식 기록. (start,end) 주면 그 창과 겹치는 밤, 없으면 max_age 안의 최근 밤."""
    import time as _t
    try:
        con = _connect(db)
        q = ("SELECT source,night_start_unix,night_end_unix,day,method,total_sleep_sec,rem_sec,deep_sec,"
             "light_sec,awake_sec,efficiency,hypnogram FROM wr_sleep_nights WHERE source='cloud' "
             "AND total_sleep_sec>=? ")
        args = [min_hours * 3600]
        if start_unix is not None and end_unix is not None:
            q += "AND night_start_unix<=? AND night_end_unix>=? "; args += [end_unix, start_unix]
        else:
            q += "AND night_end_unix>=? "; args += [_t.time() - max_age_hours * 3600]
        row = con.execute(q + "ORDER BY night_end_unix DESC LIMIT 1", args).fetchone()
        con.close()
    except Exception:
        return None
    return _night_row_to_summary(row) if row else None


def local_night_latest(db):
    try:
        con = _connect(db)
        row = con.execute(
            "SELECT source,night_start_unix,night_end_unix,day,method,total_sleep_sec,rem_sec,deep_sec,"
            "light_sec,awake_sec,efficiency,hypnogram FROM wr_sleep_nights WHERE source='local' "
            "ORDER BY night_end_unix DESC LIMIT 1").fetchone()
        con.close()
    except Exception:
        return None
    return _night_row_to_summary(row) if row else None


def nights_overview(db, min_hours=1.0, limit=60):
    """웹 날짜 이동용: 밤별로 cloud/local 요약 + 5분 에폭 시계열을 한 번에.
    같은 밤(시작 ±15분)의 두 출처를 한 항목으로 묶는다. 최신 밤이 앞."""
    try:
        con = _connect(db)
        rows = con.execute(
            "SELECT source,night_start_unix,night_end_unix,day,method,total_sleep_sec,rem_sec,deep_sec,"
            "light_sec,awake_sec,efficiency,hypnogram FROM wr_sleep_nights "
            "WHERE total_sleep_sec>=? OR source='local' ORDER BY night_start_unix DESC LIMIT ?",
            (min_hours * 3600, limit * 2)).fetchall()
        nights = []
        for r in rows:
            summ = _night_row_to_summary(r)
            eps = con.execute(
                "SELECT ts_unix,stage,hr,rmssd,motion,temp FROM wr_sleep_epochs "
                "WHERE source=? AND night_start_unix=? ORDER BY epoch_idx", (r[0], r[1])).fetchall()
            summ["epochs"] = [{"t": e[0], "s": e[1], "hr": e[2], "rmssd": e[3], "mo": e[4], "temp": e[5]}
                              for e in eps]
            summ["start_unix"] = r[1]; summ["end_unix"] = r[2]
            for n in nights:
                if abs(n["start_unix"] - r[1]) <= 900:
                    n[r[0]] = summ; n["start_unix"] = min(n["start_unix"], r[1]); break
            else:
                nights.append({"start_unix": r[1], "day": r[3], r[0]: summ})
        con.close()
    except Exception:
        return []
    return nights[:limit]


def estimate_and_store(db):
    """오늘 밤 수면창 추정 → summarize 결과(+source/method) 반환하고 wr_ 테이블에도 저장.
    wakeready/web 이 쓰는 단일 진입점. 실패 시 None."""
    win = bedtime_window(db)
    if not win:
        return None
    epochs = load_epochs(db, *win)
    stages, method = classify_with_method(epochs)
    if not stages:
        return None
    out = summarize(stages)
    out.update({"source": "local", "method": method})
    try:
        store_local_night(db, win[0], win[1], epochs, stages, method)
    except Exception:
        pass   # 저장 실패해도 추정값은 돌려준다(oura sync 와 잠금 경합 등)
    return out


def backfill(db, verbose=True):
    """DB 에 원본이 남아 있는 모든 밤을 현재 모델/휴리스틱으로 추정해 wr_ 테이블(source=local)에 저장.
    밤마다 폴링이 저장하는 건 '최근 수면창'뿐이라, 지난 밤과 모델 갱신 뒤 재추정은 이걸로 한다.
    사용: python3 scripts/sleep_estimate.py --backfill"""
    n = 0
    for s_ds, e_ds in night_windows(db):
        epochs = build_epochs(db, s_ds, e_ds)
        if sum(1 for e in epochs if e["n_beats"] > 0) < 12:
            continue
        stages, method = classify_with_method(epochs)
        if not stages:
            continue
        key = store_local_night(db, s_ds, e_ds, epochs, stages, method)
        if key is None:
            continue
        n += 1
        if verbose:
            from datetime import datetime as _dt
            m = summarize(stages)
            print(f"[✓] {_dt.fromtimestamp(key).strftime('%m-%d %H:%M')} {method:9s} "
                  f"총 {m['total_sleep_hours']}h REM {m['rem_min']} 깊은 {m['deep_min']} 깬 {m['awake_min']}분")
    if verbose:
        print(f"{n}밤 로컬 추정 저장/갱신 → wr_sleep_nights(source=local)")
    return n


def main():
    if "--backfill" in sys.argv:
        backfill(DB); return
    epochs = load_epochs(DB)
    stages = classify(epochs)
    if not stages:
        print(json.dumps({"error": "insufficient hrv_event data"})); return
    out = summarize(stages)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
