#!/usr/bin/env python3
"""web.py 의 UI(PAGE)를 '읽기 전용' 변형으로 내보낸다 → tools.creco.dev 의 /wakeready 에 그대로 쓰임.

바뀌는 것: 데이터 주소(/wakeready/api/...), '지금 동기화' 버튼 제거, "읽기 전용 · 맥에서 N분 전 전송" 표시.
UI 원본은 web.py 하나만 유지하고(두 벌 관리 안 함), 바꾼 뒤 이 스크립트로 다시 내보낸다.
사용: python3 scripts/export_readonly_ui.py <출력 .ts 경로>   (TS 모듈: export const HTML = `...`)
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
src = (ROOT / "scripts" / "web.py").read_text()
html = src[src.index('PAGE = r"""') + len('PAGE = r"""'):]
html = html[:html.index('"""')]

subs = [
    ("fetch('/api/nights?_='", "fetch('/wakeready/api/nights?_='"),
    ("fetch('/status.json?_='", "fetch('/wakeready/api/status?_='"),
    ("<title>WakeReady</title>", "<title>WakeReady · tools.creco.dev</title>"),
    # 동기화 버튼 제거 (읽기 전용)
    ('  <button class="btn" id="sync" onclick="doSync()">🔄 지금 동기화</button>\n', ""),
    # 세션 미실행 문구 → 스냅샷 없음
    ("⚠️ 세션 미실행 (tonight.sh 확인)", "⚠️ 아직 맥에서 전송된 데이터가 없어요"),
    # 갱신 시각 → 맥 전송 시각(서버 수신 기준)
    ("$('upd').textContent='갱신 '+(st.updated||'').slice(11,19);",
     "$('upd').textContent=st.received_at?('맥 전송 '+agoText(st.received_at)):'';"),
]
for a, b in subs:
    assert a in html, f"패턴 없음: {a[:50]}"
    html = html.replace(a, b)
# doSync 는 남아 있어도 무해하지만 정리
html = re.sub(r"async function doSync\(\)\{.*?\n\}\n", "", html, flags=re.S)
# 헬퍼 + 읽기 전용 배지
html = html.replace("const $=id=>document.getElementById(id);",
    "const $=id=>document.getElementById(id);\n"
    "function agoText(iso){const m=Math.round((Date.now()-new Date(iso).getTime())/60000);"
    "return m<1?'방금':(m<60?m+'분 전':(Math.round(m/60)+'시간 전'));}")
html = html.replace('<span class="tag" id="mode">—</span>',
                    '<span class="tag" id="mode">—</span>')
html = html.replace('<h1>💤 WakeReady</h1>', '<h1>💤 WakeReady <small style="font-size:12px;color:var(--muted);font-weight:500">읽기 전용</small></h1>')
assert "`" not in html and "${" not in html, "템플릿 리터럴에 넣을 수 없는 문자"
out = Path(sys.argv[1]) if len(sys.argv) > 1 else None
ts = "// 자동 생성: oura-morning-on-mac/scripts/export_readonly_ui.py — 직접 수정하지 말 것\n" \
     "export const HTML = `" + html.replace("\\", "\\\\") + "`;\n"
if out:
    out.parent.mkdir(parents=True, exist_ok=True); out.write_text(ts); print(f"[✓] {out} ({len(ts)//1024} KB)")
else:
    sys.stdout.write(ts)
