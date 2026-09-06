#!/usr/bin/env python3
"""WakeReady 웹 상태 뷰어 (의존성 0, stdlib).

wakeready.py 가 남기는 logs/status.json 을 읽어 브라우저에서 실시간 표시.
같은 와이파이의 폰/다른 기기에서 맥미니 상태를 볼 수 있다.

사용: python3 scripts/web.py            # http://<맥IP>:8777
      PORT=9000 python3 scripts/web.py
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATUS = Path(os.environ.get("LOG_DIR", str(ROOT / "logs"))) / "status.json"
PORT = int(os.environ.get("PORT", "8777"))

PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WakeReady</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;font:16px -apple-system,system-ui,sans-serif;background:#0e1116;color:#e6edf3;
 display:flex;min-height:100vh;align-items:center;justify-content:center;padding:16px}
.card{width:100%;max-width:440px;background:#161b22;border:1px solid #30363d;border-radius:16px;
 padding:22px 24px;box-shadow:0 8px 30px rgba(0,0,0,.4)}
.hd{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px}
.hd b{font-size:18px}.tag{font-size:12px;color:#7d8590;background:#21262d;padding:3px 9px;border-radius:20px}
.big{font-size:40px;font-weight:700;letter-spacing:-1px}
.sub{color:#7d8590;font-size:13px;margin-top:2px}
.bar{height:12px;background:#21262d;border-radius:6px;overflow:hidden;margin:14px 0 4px}
.bar>i{display:block;height:100%;background:linear-gradient(90deg,#3fb950,#58a6ff);border-radius:6px;transition:width .5s}
.row{display:flex;gap:10px;margin-top:14px}
.stat{flex:1;background:#0d1117;border:1px solid #30363d;border-radius:10px;padding:10px 12px}
.stat .k{font-size:11px;color:#7d8590}.stat .v{font-size:18px;font-weight:600;margin-top:2px}
.status{margin-top:16px;padding:12px 14px;background:#0d1117;border-radius:10px;border:1px solid #30363d;font-size:15px}
.foot{margin-top:14px;color:#7d8590;font-size:12px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px}
.off{color:#f85149}
</style></head><body>
<div class="card" id="card">
  <div class="hd"><b>💤 WakeReady</b><span class="tag" id="mode">—</span></div>
  <div><span class="big" id="hours">–</span><span class="sub" id="target"></span></div>
  <div class="bar"><i id="bar" style="width:0%"></i></div>
  <div class="row" id="stages"></div>
  <div class="status" id="status">연결 대기 중…</div>
  <div class="foot"><span id="cap"></span><span id="upd"></span></div>
</div>
<script>
async function tick(){
  try{
    const r = await fetch('/status.json?_='+Date.now());
    if(!r.ok) throw 0;
    const d = await r.json();
    const T = d.target_hours||8;
    document.getElementById('mode').textContent = d.mode==='healthy'?'건강모드':('총'+T+'h');
    const h = (d.hours==null?null:d.hours);
    document.getElementById('hours').textContent = h==null?'–':h.toFixed(1)+'h';
    document.getElementById('target').textContent = h==null?'':(' / '+T+'h 목표');
    document.getElementById('bar').style.width = (h==null?0:Math.min(100,100*h/T))+'%';
    const e = d.estimate, st = document.getElementById('stages');
    if(e){ st.innerHTML =
      '<div class="stat"><div class="k">REM</div><div class="v">'+e.rem_min+'분</div></div>'+
      '<div class="stat"><div class="k">깊은수면</div><div class="v">'+e.deep_min+'분</div></div>'+
      '<div class="stat"><div class="k">깬 시간</div><div class="v">'+e.awake_min+'분</div></div>';
    } else st.innerHTML='';
    document.getElementById('status').textContent = d.status||'…';
    document.getElementById('cap').textContent = '상한 '+(d.cap_time||'')+(d.next_poll?' · 다음 '+d.next_poll:'');
    document.getElementById('upd').textContent = '갱신 '+(d.updated||'').slice(11,19);
  }catch(_){
    document.getElementById('status').innerHTML='<span class="off">⚠️ 세션 미실행 또는 status.json 없음</span>';
  }
}
tick(); setInterval(tick, 5000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def _send(self, body, ctype):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/status.json"):
            try:
                self._send(STATUS.read_text(), "application/json; charset=utf-8")
            except Exception:
                self._send("{}", "application/json; charset=utf-8")
        else:
            self._send(PAGE, "text/html; charset=utf-8")

    def log_message(self, *a):
        pass  # 조용히


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    print(f"WakeReady 웹뷰: http://localhost:{PORT}  (같은 와이파이면 http://<맥IP>:{PORT})")
    print(f"상태 파일: {STATUS}")
    srv.serve_forever()
