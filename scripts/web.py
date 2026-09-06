#!/usr/bin/env python3
"""WakeReady 웹 상태 뷰어 (의존성 0, stdlib).

wakeready.py 가 남기는 logs/status.json 을 읽어 브라우저에서 실시간 표시.
같은 와이파이의 폰/다른 기기에서 맥미니 상태를 볼 수 있다.

사용: python3 scripts/web.py            # http://<맥IP>:8777
      PORT=9000 python3 scripts/web.py
"""
import json
import os
import socket
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _local_hostname():
    try:
        n = subprocess.run(["scutil", "--get", "LocalHostName"],
                           capture_output=True, text=True, timeout=3).stdout.strip()
        return n or None
    except Exception:
        return None


def _lan_ip():
    for iface in ("en0", "en1"):
        try:
            ip = subprocess.run(["ipconfig", "getifaddr", iface],
                                capture_output=True, text=True, timeout=3).stdout.strip()
            if ip:
                return ip
        except Exception:
            pass
    try:  # 폴백: 소켓으로 추정
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close()
        return ip
    except Exception:
        return None

ROOT = Path(__file__).resolve().parent.parent
LOGD = Path(os.environ.get("LOG_DIR", str(ROOT / "logs")))
STATUS = LOGD / "status.json"
SYNC_REQ = LOGD / "sync_request"   # '지금 동기화' 요청 플래그 (wakeready 가 감지)
PORT = int(os.environ.get("PORT", os.environ.get("WEB_PORT", "8777")))

PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WakeReady</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;font:16px -apple-system,system-ui,sans-serif;background:#0e1116;color:#e6edf3;
 display:flex;height:100dvh;align-items:center;justify-content:center;padding:16px;
 overflow:hidden;overscroll-behavior:none}
.card{width:100%;max-width:460px;background:#161b22;border:1px solid #30363d;border-radius:18px;
 padding:22px 22px 18px;box-shadow:0 8px 30px rgba(0,0,0,.4)}
.hd{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}
.hd b{font-size:19px}.tag{font-size:12px;color:#adbac7;background:#21262d;padding:4px 10px;border-radius:20px}
.big{font-size:44px;font-weight:800;letter-spacing:-1.5px}.big small{font-size:16px;font-weight:600;color:#7d8590}
.sub{color:#7d8590;font-size:13px;margin-top:2px}
.bar{height:14px;background:#21262d;border-radius:7px;overflow:hidden;margin:14px 0 2px;position:relative}
.bar>i{display:block;height:100%;background:linear-gradient(90deg,#3fb950,#58a6ff);border-radius:7px;transition:width .5s}
.qual{margin-top:12px;font-size:15px;font-weight:600}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:14px}
.stat{background:#0d1117;border:1px solid #30363d;border-radius:12px;padding:11px 13px}
.stat .k{font-size:11px;color:#7d8590}.stat .v{font-size:19px;font-weight:700;margin-top:3px}
.stat .t{font-size:11px;color:#6e7681;margin-top:2px}
.mini{height:5px;background:#21262d;border-radius:3px;overflow:hidden;margin-top:7px}
.mini>i{display:block;height:100%;border-radius:3px}
.status{margin-top:16px;padding:13px 15px;background:#0d1117;border-radius:12px;border:1px solid #30363d;font-size:15px;line-height:1.4}
.btn{display:block;width:100%;margin-top:14px;padding:14px;border:0;border-radius:12px;
 background:#238636;color:#fff;font-size:16px;font-weight:700;cursor:pointer}
.btn:active{background:#2ea043}.btn:disabled{background:#30363d;color:#7d8590}
.foot{margin-top:14px;color:#7d8590;font-size:12px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px}
.off{color:#f85149}.ok{color:#3fb950}
</style></head><body>
<div class="card">
  <div class="hd"><b>💤 WakeReady</b><span class="tag" id="mode">—</span></div>
  <div><span class="big"><span id="hours">–</span><small id="target"></small></span></div>
  <div class="sub" id="remain"></div>
  <div class="bar"><i id="bar" style="width:0%"></i></div>
  <div class="qual" id="qual"></div>
  <div class="grid" id="stages"></div>
  <div class="status" id="status">연결 대기 중…</div>
  <button class="btn" id="sync" onclick="doSync()">🔄 지금 동기화</button>
  <div class="foot"><span id="meta"></span><span id="upd"></span></div>
</div>
<script>
function eff(e){const a=e.rem_min+e.deep_min+e.light_min;const t=a+e.awake_min;return t?Math.round(100*a/t):0;}
function quality(e){const ef=eff(e);
  if(e.rem_pct>=18&&e.deep_pct>=13&&ef>=85)return['😴 잘 자는 중','#3fb950'];
  if(e.rem_pct>=13&&e.deep_pct>=10&&ef>=78)return['🙂 양호','#58a6ff'];
  return['😐 뒤척임 많음','#d29922'];}
function tile(k,v,t,frac,col){
  return '<div class="stat"><div class="k">'+k+'</div><div class="v">'+v+'</div>'+
   (t?'<div class="t">'+t+'</div>':'')+
   (frac!=null?'<div class="mini"><i style="width:'+Math.min(100,frac*100)+'%;background:'+col+'"></i></div>':'')+
   '</div>';}
async function tick(){
  try{
    const r=await fetch('/status.json?_='+Date.now()); if(!r.ok)throw 0;
    const d=await r.json(); const T=d.target_hours||8; const h=(d.hours==null?null:d.hours);
    mode.textContent=d.mode==='healthy'?'건강 수면 모드':('총 '+T+'h 모드');
    // 데이터가 있을 때만 숫자 교체. 동기화 중(null)이면 이전 화면 유지(깜빡임 방지).
    if(h!=null){
      hours.textContent=h.toFixed(1); target.textContent=' / '+T+'h';
      bar.style.width=Math.min(100,100*h/T)+'%';
      remain.textContent='목표까지 '+Math.max(0,(T-h)).toFixed(1)+'h 남음';
    }
    const e=d.estimate;
    if(e){const[ql,qc]=quality(e); qual.innerHTML='<span style="color:'+qc+'">'+ql+'</span> · 효율 '+eff(e)+'%';
      const remT=d.rem_min_target, deepT=d.deep_min_target;
      stages.innerHTML=
        tile('REM 수면',e.rem_min+'분',d.mode==='healthy'?('목표 '+remT+'분'):(e.rem_pct+'%'),
             d.mode==='healthy'?e.rem_min/remT:null,'#bc8cff')+
        tile('깊은 수면',e.deep_min+'분',d.mode==='healthy'?('목표 '+deepT+'분'):(e.deep_pct+'%'),
             d.mode==='healthy'?e.deep_min/deepT:null,'#58a6ff')+
        tile('얕은 수면',e.light_min+'분',e.light_pct+'%',null,null)+
        tile('깬 시간',e.awake_min+'분',null,null,null);
    }  // estimate 없어도 이전 타일 유지(지우지 않음)
    const stale=(d.status||'').indexOf('지난')>=0;
    let s=(stale?'⏸️ ':'')+(d.status||'…');
    if(d.phase==='syncing'){ s+=' <span class="ok">(동기화 중…)</span>';
      if((d.fails||0)>=2) s+='<br><span class="off">링 연결 실패 '+d.fails+'회 — 링 착용·맥 근처·아이폰 BT OFF 확인</span>'; }
    status.innerHTML=s;
    meta.textContent='상한 '+(d.cap_time||'')+(d.next_poll?' · 다음 '+d.next_poll:'');
    upd.textContent='갱신 '+(d.updated||'').slice(11,19);
  }catch(_){ status.innerHTML='<span class="off">⚠️ 세션 미실행 (tonight.sh 확인)</span>'; }
}
async function doSync(){
  const b=document.getElementById('sync');
  b.disabled=true; b.textContent='요청됨 — 곧 동기화…';
  try{ await fetch('/sync',{method:'POST'}); }catch(_){}
  setTimeout(()=>{b.disabled=false;b.textContent='🔄 지금 동기화';},8000);
  tick();
}
tick(); setInterval(tick,5000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def _send(self, body, ctype):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")   # 옛 페이지 캐시 방지
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

    def do_POST(self):
        if self.path.startswith("/sync"):
            try:
                LOGD.mkdir(parents=True, exist_ok=True)
                SYNC_REQ.write_text(str(int(__import__("time").time())))
                self._send('{"ok":true}', "application/json; charset=utf-8")
            except Exception:
                self._send('{"ok":false}', "application/json; charset=utf-8")
        else:
            self._send('{"ok":false}', "application/json; charset=utf-8")

    def log_message(self, *a):
        pass  # 조용히


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    host = _local_hostname()
    ip = _lan_ip()
    bar = "─" * 46
    print(bar)
    print("  WakeReady 웹뷰 실행 중 — 같은 와이파이에서 접속:")
    if host:
        print(f"    ▶  http://{host}.local:{PORT}      (권장·주소 고정)")
    if ip:
        print(f"    ▶  http://{ip}:{PORT}")
    print(f"    ▶  http://localhost:{PORT}          (이 맥에서)")
    print(bar)
    # 폰 카메라로 스캔하면 바로 열리는 QR
    url = f"http://{host}.local:{PORT}" if host else (f"http://{ip}:{PORT}" if ip else None)
    if url:
        try:
            import sys as _sys
            _sys.path.insert(0, str(Path(__file__).resolve().parent))
            from _qr import qr_ascii
            q = qr_ascii(url)
            if q:
                print("  📷 폰 카메라로 스캔:")
                print(q)
            else:
                print("  (QR 보려면: pip install qrcode)")
        except Exception:
            pass
    print(f"  상태 파일: {STATUS}")
    print("  (Ctrl+C 로 종료)")
    srv.serve_forever()
