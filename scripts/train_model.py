#!/usr/bin/env python3
"""#2 개인화 수면단계 모델 학습 + 검증 리포트.

정답: data/training/labels_api_*.json (fetch_labels_api.py, 공식 5분 히프노그램)
      또는 labels_*.json (collect_labels.py, 30초 → 5분 다수결로 변환)
신호: data/oura.db 의 각 밤 원시 이벤트(IBI·모션·체온). 라벨의 bedtime_start(unix)를
      링 시계로 환산(time_sync 오프셋)해 같은 밤을 찾고, 5분 격자를 bedtime_start 에서
      시작시켜 라벨 인덱스와 1:1 로 절대시각 정렬한다. (상대위치 리샘플 아님)
검증: 밤 하나 빼기(leave-one-night-out). 각 밤에 대해 휴리스틱 vs 모델의
      에폭 일치율 + REM/깊은수면/깬 시간 총분 오차를 출력해 임계값을 믿을 수 있는지 보인다.
출력: models/sleep_clf.pkl (있으면 sleep_estimate 가 자동 사용), models/train_report.json

사용: python3 scripts/train_model.py [--no-save] [--db PATH]
필요: pip install scikit-learn   (.venv 에 있음)
주의: 5밤 이하면 참고용. 2~3주 쌓이면 쓸만해진다. 라벨/모델은 내 데이터 → gitignore.
"""
import glob
import json
import os
import pickle
import sys

os.environ.setdefault("LOKY_MAX_CPU_COUNT", "2")   # joblib 이 sysctl 못 찾아 내는 경고 억제
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRAIN_DIR = ROOT / "data" / "training"
MODEL_OUT = ROOT / "models" / "sleep_clf.pkl"
REPORT_OUT = ROOT / "models" / "train_report.json"
STAGES = ["DEEP", "LIGHT", "REM", "WAKE"]
MIN_NIGHTS = 3


def _import_estimator():
    import importlib.util
    spec = importlib.util.spec_from_file_location("se", ROOT / "scripts" / "sleep_estimate.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def _to_5min(stages, epoch_sec):
    """30초 라벨이면 10개씩 다수결로 5분으로 합친다."""
    if epoch_sec >= 300:
        return stages
    k = max(1, 300 // epoch_sec)
    return [Counter(stages[i:i + k]).most_common(1)[0][0] for i in range(0, len(stages), k)]


def load_labels():
    out = []
    for lf in sorted(glob.glob(str(TRAIN_DIR / "labels_*.json"))):
        lab = json.loads(Path(lf).read_text())
        stages = lab.get("stages") or lab.get("stages_30s")
        start = lab.get("bedtime_start")
        if not stages or not start:
            continue
        try:
            start_unix = datetime.fromisoformat(start).timestamp()
        except Exception:
            continue
        stages = _to_5min(stages, int(lab.get("epoch_sec", 300)))
        out.append({"file": Path(lf).name, "day": lab.get("day") or start[:10],
                    "start_unix": start_unix, "stages": stages,
                    "official": lab.get("official_min") or {}})
    return out


def minutes(stages):
    return {k: 5 * sum(1 for s in stages if s == k) for k in STAGES}


def night_dataset(se, db, offset, lab):
    """라벨 밤 ↔ DB 신호 정렬. (features, labels, epochs) 또는 None(신호 없음)."""
    start_ds = int(round((lab["start_unix"] - offset) * 10))
    end_ds = start_ds + len(lab["stages"]) * 5 * 60 * 10
    epochs = se.build_epochs(db, start_ds, end_ds)
    if sum(1 for e in epochs if e["n_beats"] > 0) < 12:   # 1시간 미만이면 그 밤은 DB 에 없음
        return None
    feats = se.epoch_features(epochs)
    n = min(len(feats), len(lab["stages"]))
    return feats[:n], lab["stages"][:n], epochs[:n]


# 2026-09-09 5밤 LOO 비교로 고른 설정: 얕은 트리(depth2)·느린 학습(300회, lr .03)·균형 가중치
# + 비터비(self_bias 0.5). 에폭 일치율 66→72%, REM/깊은 총분 절대오차 46/39 → 28/26분.
SELF_BIAS = 0.5


def make_clf():
    from sklearn.ensemble import HistGradientBoostingClassifier
    return HistGradientBoostingClassifier(max_depth=2, max_iter=300, learning_rate=0.03,
                                          min_samples_leaf=8, l2_regularization=1.0,
                                          class_weight="balanced", random_state=0)


def transitions(seqs, classes):
    """라벨 시퀀스에서 단계 전이확률(add-one 평활). classes 순서(=clf.classes_)로 반환."""
    c = {a: {b: 1.0 for b in classes} for a in classes}
    for s in seqs:
        for a, b in zip(s, s[1:]):
            if a in c and b in c[a]:
                c[a][b] += 1
    return [[c[a][b] / sum(c[a].values()) for b in classes] for a in classes]


def fit(se, rows):
    """rows: [(features, stage)] 를 밤 경계 없이 이어 붙인 것. 전이행렬은 별도로 seqs 에서."""
    X = [[r[k] for k in se.FEATURES] for r, _ in rows if r["has_data"]]
    y = [s for r, s in rows if r["has_data"]]
    clf = make_clf(); clf.fit(X, y)
    return clf


def bundle_of(clf, seqs, se):
    classes = [str(c) for c in clf.classes_]
    return {"clf": clf, "features": se.FEATURES, "trans": transitions(seqs, classes),
            "self_bias": SELF_BIAS}


def predict(se, bundle, feats):
    """sleep_estimate.classify_model 과 같은 경로: 확률 → 비터비 → 결측은 WAKE."""
    clf = bundle["clf"]
    X = [[r[k] for k in se.FEATURES] for r in feats]
    pred = se.viterbi(clf.predict_proba(X), [str(c) for c in clf.classes_], bundle["trans"],
                      bundle.get("self_bias", 0.0))
    pred = [("WAKE" if r["has_data"] == 0 else s) for r, s in zip(feats, pred)]
    return se._smooth(pred)


def heuristic(se, epochs):
    """모델 파일이 있어도 순수 휴리스틱만 돌린다(비교용)."""
    saved = se.MODEL_PKL
    se.MODEL_PKL = Path("/nonexistent")
    try:
        return se.classify(epochs) or ["WAKE"] * len(epochs)
    finally:
        se.MODEL_PKL = saved


def acc(pred, truth):
    n = len(truth)
    return round(100 * sum(1 for p, t in zip(pred, truth) if p == t) / n, 1) if n else 0


def err(pred, truth):
    pm, tm = minutes(pred), minutes(truth)
    return {k: pm[k] - tm[k] for k in ("REM", "DEEP", "WAKE")}


def main():
    save = "--no-save" not in sys.argv
    db = str(ROOT / "data" / "oura.db")
    if "--db" in sys.argv:
        db = sys.argv[sys.argv.index("--db") + 1]
    se = _import_estimator()
    offset = se.ring_offset(db)
    if offset is None:
        print("DB 에 time_sync 이벤트가 없어 링 시계를 실제 시각으로 환산할 수 없음."); sys.exit(1)
    labels = load_labels()
    if not labels:
        print(f"라벨 없음: {TRAIN_DIR}/labels_*.json — fetch_labels_api.py 로 먼저 수집하세요."); sys.exit(1)

    nights = []
    for lab in labels:
        ds = night_dataset(se, db, offset, lab)
        if ds is None:
            print(f"  {lab['day']}: DB 에 그 밤 신호 없음 — 건너뜀"); continue
        feats, truth, epochs = ds
        covered = sum(1 for f in feats if f["has_data"])
        print(f"  {lab['day']}: 라벨 {len(lab['stages'])}에폭, DB 신호 {covered}에폭 정렬됨"
              + (f" (DB 는 {5*covered//60}h{5*covered%60:02d}m 까지만)" if covered < len(lab['stages']) - 2 else ""))
        nights.append({"day": lab["day"], "feats": feats, "truth": truth, "epochs": epochs})
    if len(nights) < MIN_NIGHTS:
        print(f"정렬된 밤이 {len(nights)}개 — 최소 {MIN_NIGHTS}밤 필요. 며칠 더 모으세요."); sys.exit(1)
    try:
        make_clf()
    except ImportError:
        print("scikit-learn 필요: pip install scikit-learn  (또는 .venv/bin/python 으로 실행)"); sys.exit(1)

    # ── 밤 하나 빼기 검증 ──────────────────────────────────────────────
    print("\n밤 하나 빼기 검증 (에폭 일치율 % / 총분 오차 = 추정−공식, 분)")
    print(f"{'밤':10s} {'휴리스틱':>8s} {'모델':>6s} | {'REM(휴/모)':>13s} {'깊은(휴/모)':>13s} {'깬(휴/모)':>13s}  공식 REM/깊은/깬")
    report = []
    sum_h = sum_m = 0
    abs_h = Counter(); abs_m = Counter()
    for i, n in enumerate(nights):
        train_rows = [(f, s) for j, m in enumerate(nights) if j != i for f, s in zip(m["feats"], m["truth"])]
        clf = fit(se, train_rows)
        b = bundle_of(clf, [m["truth"] for j, m in enumerate(nights) if j != i], se)
        valid = [k for k, f in enumerate(n["feats"]) if f["has_data"]]
        truth = [n["truth"][k] for k in valid]
        pm = predict(se, b, n["feats"]); ph = heuristic(se, n["epochs"])
        pm_v = [pm[k] for k in valid]; ph_v = [ph[k] for k in valid]
        a_h, a_m = acc(ph_v, truth), acc(pm_v, truth)
        e_h, e_m = err(ph_v, truth), err(pm_v, truth)
        tm = minutes(truth)
        sum_h += a_h; sum_m += a_m
        for k in e_h: abs_h[k] += abs(e_h[k]); abs_m[k] += abs(e_m[k])
        print(f"{n['day']:10s} {a_h:8.1f} {a_m:6.1f} | {e_h['REM']:+5d}/{e_m['REM']:+5d}   "
              f"{e_h['DEEP']:+5d}/{e_m['DEEP']:+5d}   {e_h['WAKE']:+5d}/{e_m['WAKE']:+5d}   "
              f"{tm['REM']}/{tm['DEEP']}/{tm['WAKE']}")
        report.append({"day": n["day"], "acc_heuristic": a_h, "acc_model": a_m,
                       "err_heuristic": e_h, "err_model": e_m, "official": tm,
                       "epochs_used": len(valid)})
    k = len(nights)
    print(f"{'평균':10s} {sum_h/k:8.1f} {sum_m/k:6.1f} | "
          f"{abs_h['REM']/k:5.0f}/{abs_m['REM']/k:5.0f}    {abs_h['DEEP']/k:5.0f}/{abs_m['DEEP']/k:5.0f}    "
          f"{abs_h['WAKE']/k:5.0f}/{abs_m['WAKE']/k:5.0f}   (절대오차 평균)")

    # ── 전체 학습 + 저장 ───────────────────────────────────────────────
    all_rows = [(f, s) for n in nights for f, s in zip(n["feats"], n["truth"])]
    clf = fit(se, all_rows)
    final = bundle_of(clf, [n["truth"] for n in nights], se)
    n_samples = sum(1 for f, _ in all_rows if f["has_data"])
    summary = {"trained_at": datetime.now().isoformat(timespec="seconds"), "nights": k,
               "samples": n_samples, "features": se.FEATURES,
               "cv_acc_heuristic": round(sum_h / k, 1), "cv_acc_model": round(sum_m / k, 1),
               "cv_abs_err_model": {kk: round(abs_m[kk] / k) for kk in abs_m},
               "cv_abs_err_heuristic": {kk: round(abs_h[kk] / k) for kk in abs_h},
               "per_night": report}
    if not save:
        print("\n--no-save: 모델 저장 안 함"); return
    MODEL_OUT.parent.mkdir(parents=True, exist_ok=True)
    better = sum_m >= sum_h
    if better:
        with open(MODEL_OUT, "wb") as f:
            pickle.dump(dict(final, meta=summary), f)
        print(f"\n[✓] 모델 저장 → {MODEL_OUT}  ({k}밤, {n_samples}에폭) — sleep_estimate 가 자동 사용")
    else:
        if MODEL_OUT.exists():
            MODEL_OUT.unlink()
        print(f"\n[!] 모델이 휴리스틱보다 못함 → 저장 안 함(기존 모델 제거). 휴리스틱 계속 사용")
    summary["model_saved"] = better
    REPORT_OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"    리포트 → {REPORT_OUT}")


if __name__ == "__main__":
    main()
