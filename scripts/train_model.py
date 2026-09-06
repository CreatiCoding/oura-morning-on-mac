#!/usr/bin/env python3
"""#2 개인화 수면단계 모델 학습.

입력: data/training/labels_*.json (collect_labels.py 산출, 정답 30초 히프노그램)
      + 각 밤의 원시신호(oura.db 의 최신 bedtime_period 창 에폭들)
정렬: 라벨은 unix, 신호는 링 deciseconds라 절대시각 대신 '밤 내 상대위치'로 정렬.
출력: models/sleep_clf.pkl (개인화 분류기). 있으면 sleep_estimate 가 자동 사용.

필요: pip install scikit-learn
주의: 며칠~2주치 라벨이 쌓여야 쓸만해진다. 라벨/모델은 내 데이터 → gitignore.
"""
import glob
import json
import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRAIN_DIR = ROOT / "data" / "training"
MODEL_OUT = ROOT / "models" / "sleep_clf.pkl"
FEATURES = ["hr", "rmssd", "motion", "frac", "hr_z", "rm_z"]
STAGES = ["DEEP", "LIGHT", "REM", "WAKE"]


def _import_estimator():
    import importlib.util
    spec = importlib.util.spec_from_file_location("se", ROOT / "scripts" / "sleep_estimate.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def resample(seq, n):
    """seq 를 n개로 상대위치 리샘플(최근접)."""
    if not seq:
        return []
    return [seq[min(len(seq) - 1, int(round(k * (len(seq) - 1) / max(1, n - 1))))]
            for k in range(n)]


def main():
    se = _import_estimator()
    db = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "data" / "oura.db")
    label_files = sorted(glob.glob(str(TRAIN_DIR / "labels_*.json")))
    if not label_files:
        print(f"라벨 없음: {TRAIN_DIR}/labels_*.json — collect_labels.py 로 먼저 수집하세요.")
        sys.exit(1)

    X, y = [], []
    # 현재 DB의 창으로 피처 추출(밤마다 별도 DB가 이상적이나, 우선 최신 창 사용)
    for lf in label_files:
        lab = json.loads(Path(lf).read_text())
        stages30 = lab["stages_30s"]
        epochs = se.load_epochs(db)   # 최신 창 에폭 (밤별 DB 스냅샷이면 더 정확)
        feats = se.epoch_features(epochs)
        if len(feats) < 5:
            continue
        N = len(feats)
        labels_rs = resample(stages30, N)   # 라벨을 피처 개수에 맞춰 상대위치 정렬
        for f, s in zip(feats, labels_rs):
            X.append([f[k] for k in FEATURES]); y.append(s)
        print(f"  {Path(lf).name}: {N} epochs")

    if len(set(y)) < 2 or len(X) < 20:
        print(f"학습 데이터 부족(샘플 {len(X)}, 클래스 {set(y)}). 며칠 더 모으세요.")
        sys.exit(1)

    try:
        from sklearn.ensemble import HistGradientBoostingClassifier
    except ImportError:
        print("scikit-learn 필요: pip install scikit-learn"); sys.exit(1)
    clf = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.1)
    clf.fit(X, y)
    MODEL_OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(MODEL_OUT, "wb") as f:
        pickle.dump({"clf": clf, "features": FEATURES}, f)
    print(f"[✓] 모델 저장 → {MODEL_OUT}  (샘플 {len(X)}, 클래스 {sorted(set(y))})")
    print("    이제 sleep_estimate 가 이 모델을 자동 사용합니다.")


if __name__ == "__main__":
    main()
