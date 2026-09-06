#!/usr/bin/env python3
"""#2 개인화 모델용 '정답 라벨' 수집.

Oura가 계산한 공식 수면단계(히프노그램)를 한 밤 단위로 저장한다. 이게 학습의 정답.
소스: assa.sqlite (iPhone 백업에서 추출한 '내 건강데이터'). 30초 단위 stage 문자열을 읽어
자동으로 digit→stage 매핑을 유추(그 행의 rem/deep/light/awake 지속시간과 대조)한다.

사용: python3 collect_labels.py <assa.sqlite> [out_dir=data/training]
출력: data/training/labels_<bedtime_start>.json  (30초 stage 배열 + 창 + 지속시간)

주의: 라벨은 '내 데이터'다. 학습은 Oura 출력을 목표로 하는 것이며, 독점 모델/키는 안 건드린다.
"""
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGES = ["DEEP", "LIGHT", "REM", "WAKE"]


def infer_mapping(phase_str, durations):
    """digit→stage 매핑을 지속시간(분)과 digit 개수 대조로 유추.
    durations: {'DEEP':분,'LIGHT':분,'REM':분,'WAKE':분}. 각 digit=30초."""
    counts = Counter(phase_str)
    # digit별 시간(분) = count * 0.5
    digit_min = {d: c * 0.5 for d, c in counts.items()}
    # 지속시간이 큰 stage부터, digit 시간이 큰 것부터 매칭 (greedy)
    order_stage = sorted(durations, key=lambda s: -durations[s])
    order_digit = sorted(digit_min, key=lambda d: -digit_min[d])
    mapping = {}
    for stage, digit in zip(order_stage, order_digit):
        mapping[digit] = stage
    return mapping


def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(2)
    assa = sys.argv[1]
    out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "data" / "training"
    out_dir.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(assa)
    rows = con.execute(
        "SELECT bedtime_start, bedtime_end, sleep_phase_30_sec, "
        "rem_sleep_duration, deep_sleep_duration, light_sleep_duration, awake_time "
        "FROM sleep WHERE sleep_phase_30_sec IS NOT NULL AND total_sleep_duration > 3600 "
        "ORDER BY bedtime_start"
    ).fetchall()
    con.close()

    saved = 0
    for bs, be, phase, rem, deep, light, awake in rows:
        if not phase:
            continue
        durations = {"REM": (rem or 0) / 60, "DEEP": (deep or 0) / 60,
                     "LIGHT": (light or 0) / 60, "WAKE": (awake or 0) / 60}
        mapping = infer_mapping(phase, durations)
        stages30 = [mapping.get(ch, "LIGHT") for ch in phase]
        rec = {
            "bedtime_start": bs, "bedtime_end": be,
            "stages_30s": stages30,       # 30초 단위 stage 배열 (정답)
            "digit_mapping": mapping,
            "official_min": {k: round(v) for k, v in durations.items()},
        }
        fn = out_dir / f"labels_{bs}.json"
        fn.write_text(json.dumps(rec, ensure_ascii=False))
        saved += 1
        print(f"[✓] {fn.name}  REM{rec['official_min']['REM']} "
              f"DEEP{rec['official_min']['DEEP']} LIGHT{rec['official_min']['LIGHT']} "
              f"WAKE{rec['official_min']['WAKE']}  map={mapping}")
    print(f"\n총 {saved}밤 라벨 저장 → {out_dir}")


if __name__ == "__main__":
    main()
