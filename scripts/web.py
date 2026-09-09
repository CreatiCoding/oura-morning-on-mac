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
DB = Path(os.environ.get("DB", str(ROOT / "data" / "oura.db")))
import time as _time

# 세션이 안 돌아 status.json 이 비거나 오래됐을 때, DB 에서 직접 수면단계를 추정해
# UI 를 채우기 위한 헬퍼. wakeready.py 의 함수를 재사용(링 통신 없이 DB 만 읽음). 60초 캐시.
_DBCACHE = {"t": 0.0, "data": None}
_WK = None

def _db_snapshot():
    global _WK
    now = _time.time()
    if _DBCACHE["data"] is not None and now - _DBCACHE["t"] < 60:
        return _DBCACHE["data"]
    data = {"hours": None, "estimate": None}
    try:
        if _WK is None:
            import importlib.util
            spec = importlib.util.spec_from_file_location("wakeready_web", ROOT / "scripts" / "wakeready.py")
            m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
            _WK = m
        est = _WK.estimate_stages()
        bp = _WK.latest_bedtime_period()
        hours = (est.get("total_sleep_hours") if est else None)
        if hours is None and bp:
            try:
                hours = float(bp["duration_hours"])
            except Exception:
                hours = None
        data = {"hours": hours, "estimate": est}
    except Exception:
        pass
    _DBCACHE.update(t=now, data=data)
    return data

_SE = None
def _se():
    global _SE
    if _SE is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("se_web", ROOT / "scripts" / "sleep_estimate.py")
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        _SE = m
    return _SE


# 클라우드(공식 API) 기록 갱신: 페이지를 보는 동안 최대 30분에 1번, fetch_labels_api.py 를 백그라운드로.
# 토큰이 없으면(.env 에 OURA_API_TOKEN) 아무것도 안 함. 실패해도 UI 는 로컬 추정으로 계속.
CLOUD_REFRESH_SEC = int(os.environ.get("CLOUD_REFRESH_SEC", "1800"))
_CLOUD = {"t": 0.0, "proc": None}

def _has_token():
    if os.environ.get("OURA_API_TOKEN"):
        return True
    try:
        return any(l.startswith("OURA_API_TOKEN=") and len(l) > 16
                   for l in (ROOT / ".env").read_text().splitlines())
    except Exception:
        return False

def _maybe_refresh_cloud():
    now = _time.time()
    if now - _CLOUD["t"] < CLOUD_REFRESH_SEC or not _has_token():
        return
    if _CLOUD["proc"] is not None and _CLOUD["proc"].poll() is None:
        return
    _CLOUD["t"] = now
    try:
        import sys
        from datetime import date, timedelta
        LOGD.mkdir(parents=True, exist_ok=True)
        out = open(LOGD / "labels.out", "a")
        _CLOUD["proc"] = subprocess.Popen(
            [sys.executable, str(ROOT / "scripts" / "fetch_labels_api.py"),
             str(date.today() - timedelta(days=3)), str(date.today())],
            stdout=out, stderr=subprocess.STDOUT, cwd=str(ROOT))
    except Exception:
        pass


def _current_night_unix():
    """로컬 DB 의 최신 수면창을 unix 로. (start, end) 또는 None."""
    try:
        se = _se(); win = se.bedtime_window(str(DB)); off = se.ring_offset(str(DB))
        if win and off is not None:
            return int(off + win[0] / 10), int(off + win[1] / 10)
    except Exception:
        pass
    return None


def status_payload():
    """status.json(세션 상태) + 표시 데이터 선택.
    표시 우선순위: ① 이 밤의 클라우드 공식 기록(있으면)  ② 로컬 추정(모델/휴리스틱).
    응답에 source('cloud'|'local'), method, source_label 을 넣어 UI 가 출처를 표기한다.
    판정(알람)은 항상 로컬 기준이며 여기서 바뀌지 않는다 — 표시만 고른다."""
    try:
        d = json.loads(STATUS.read_text())
    except Exception:
        d = {}
    _maybe_refresh_cloud()
    # ② 로컬 추정으로 빈 칸 채우기 (세션이 안 돌 때)
    if d.get("estimate") is None or d.get("hours") is None:
        snap = _db_snapshot()
        if d.get("estimate") is None and snap["estimate"] is not None:
            d["estimate"] = snap["estimate"]
            d["from_db"] = True   # 라이브 폴링이 아니라 DB 추정으로 채운 값
        if d.get("hours") is None and snap["hours"] is not None:
            d["hours"] = snap["hours"]
        d.setdefault("target_hours", 8.0)
        d.setdefault("mode", "total")
        if not d.get("status"):
            d["status"] = "참고: 마지막 동기화 기준 추정 (세션 대기 중)"
    est = d.get("estimate") or {}
    d["source"] = "local"; d["method"] = est.get("method")
    d["source_label"] = "💍 로컬 추정 · " + ("개인화 모델" if est.get("method") == "model" else "휴리스틱")
    # ① 이 밤에 해당하는 클라우드 공식 기록이 있으면 그걸 표시
    try:
        win = _current_night_unix()
        cloud = _se().cloud_night_for(str(DB), *(win or (None, None)))
        if cloud:
            d["local_estimate"] = d.get("estimate")      # 비교용으로 로컬 값도 같이 보냄
            d["estimate"] = cloud
            d["hours"] = cloud["total_sleep_hours"]
            d["source"] = "cloud"; d["method"] = "oura_api"
            d["source_label"] = f"☁️ Oura 공식 기록 · {cloud['day']} ({cloud['bedtime_start'][11:16]}→{(cloud['bedtime_end'] or '')[11:16]})"
    except Exception:
        pass
    return json.dumps(d, ensure_ascii=False)
PORT = int(os.environ.get("PORT", os.environ.get("WEB_PORT", "8777")))

PAGE = r"""<!doctype html><html lang="ko" style="color-scheme:dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0e1116">
<title>WakeReady</title>
<style>
:root{--page:#0e1116;--surface:#161b22;--surface2:#0d1117;--border:#30363d;--ink:#e6edf3;--ink2:#adbac7;--muted:#7d8590;
 --grid:#21262d;--accent:#3987e5;--accent-track:#183a66;--good:#3fb950;--warn:#d29922;--bad:#f85149;
 --wake:#d95926;--rem:#9085e9;--light:#199e70;--deep:#3987e5}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{min-height:100%}
body{margin:0;font:16px system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--page);color:var(--ink);
 padding:calc(12px + env(safe-area-inset-top)) 12px calc(20px + env(safe-area-inset-bottom));touch-action:manipulation}
.wrap{max-width:480px;margin:0 auto;display:flex;flex-direction:column;gap:12px}
h1{font-size:19px;margin:0;font-weight:700;text-wrap:balance}
.card{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:16px}
.hd{display:flex;justify-content:space-between;align-items:center;gap:8px}
.tag{font-size:12px;color:var(--ink2);background:var(--grid);padding:4px 10px;border-radius:20px;white-space:nowrap}
/* 필터 행: 날짜 이동 + 출처 토글 (차트 위 한 줄) */
.filters{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.nav{display:flex;align-items:center;gap:4px;flex:1;min-width:0}
.ib{width:40px;height:40px;border:1px solid var(--border);background:var(--surface);color:var(--ink);border-radius:10px;
 font-size:18px;cursor:pointer;display:grid;place-items:center;transition:background-color .15s}
.ib:hover{background:var(--grid)}.ib:disabled{opacity:.35;cursor:default}
select.date{flex:1;min-width:0;height:40px;border:1px solid var(--border);border-radius:10px;background:var(--surface);color:var(--ink);
 font:600 15px system-ui,-apple-system,sans-serif;padding:0 10px;appearance:none;-webkit-appearance:none;text-align:center}
.seg{display:flex;border:1px solid var(--border);border-radius:10px;overflow:hidden;background:var(--surface)}
.seg button{height:40px;padding:0 12px;border:0;background:transparent;color:var(--ink2);font:600 14px system-ui,-apple-system,sans-serif;
 cursor:pointer;transition:background-color .15s,color .15s;white-space:nowrap}
.seg button[aria-checked="true"]{background:var(--grid);color:var(--ink)}
.seg button:disabled{opacity:.35;cursor:default}
.live{font-size:12px;color:var(--good);display:inline-flex;align-items:center;gap:5px}
.live::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--good)}
/* 헤드라인 */
.hero{font-size:52px;font-weight:800;letter-spacing:-1.5px;line-height:1}
.hero small{font-size:17px;font-weight:600;color:var(--muted);letter-spacing:0}
.sub{color:var(--muted);font-size:13px;margin-top:6px}
.meter{height:12px;background:var(--accent-track);border-radius:6px;overflow:hidden;margin:14px 0 4px}
.meter>i{display:block;height:100%;background:var(--accent);border-radius:6px;transition:width .4s}
.qual{margin-top:10px;font-size:15px;font-weight:600}
.src{margin-top:12px;font-size:12px;color:var(--ink2);background:var(--grid);border-radius:8px;padding:7px 10px;display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap}
.src.cloud{background:#12261b;color:#7ee2a8}.src small{color:var(--muted)}
/* 차트 */
.ct{font-size:13px;color:var(--ink2);font-weight:600;display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:8px}
.legend{display:flex;gap:10px;flex-wrap:wrap;font-size:11px;color:var(--muted);font-weight:500}
.legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:4px;vertical-align:-1px}
.chart{position:relative}
.chart svg{display:block;width:100%;height:auto;outline:none;touch-action:pan-y}
.chart svg:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:6px}
.tip{position:absolute;top:0;left:0;pointer-events:none;background:#0d1117;border:1px solid var(--border);border-radius:8px;padding:6px 9px;
 font-size:12px;line-height:1.35;color:var(--ink2);white-space:nowrap;opacity:0;transition:opacity .12s;transform:translate(-50%,0)}
.tip b{color:var(--ink);font-size:13px}.tip.on{opacity:1}
.axis{font-size:10px;fill:var(--muted);font-variant-numeric:tabular-nums}
.ylab{font-size:10px;fill:var(--muted)}
.empty{color:var(--muted);font-size:13px;padding:18px 0;text-align:center}
/* KPI 타일 */
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.stat{background:var(--surface2);border:1px solid var(--border);border-radius:12px;padding:11px 13px}
.stat .k{font-size:11px;color:var(--muted);display:flex;align-items:center;gap:5px}
.stat .k i{width:8px;height:8px;border-radius:2px;display:inline-block}
.stat .v{font-size:20px;font-weight:700;margin-top:3px}
.stat .t{font-size:11px;color:var(--muted);margin-top:2px;min-height:14px}
.stat .d{font-size:11px;color:var(--ink2);margin-top:2px;min-height:14px}
.mini{height:5px;background:var(--grid);border-radius:3px;overflow:hidden;margin-top:7px}
.mini>i{display:block;height:100%;border-radius:3px;transition:width .4s}
/* 상태/버튼 */
.status{padding:13px 15px;background:var(--surface2);border-radius:12px;border:1px solid var(--border);font-size:15px;line-height:1.4}
.btn{display:block;width:100%;margin-top:12px;padding:14px;border:0;border-radius:12px;background:#238636;color:#fff;font-size:16px;font-weight:700;cursor:pointer;transition:background-color .15s}
.btn:hover{background:#2ea043}.btn:disabled{background:var(--border);color:var(--muted)}
.foot{color:var(--muted);font-size:12px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px}
.off{color:var(--bad)}.ok{color:var(--good)}
details.tbl{margin-top:10px}details.tbl summary{font-size:12px;color:var(--muted);cursor:pointer;padding:6px 0}
table{width:100%;border-collapse:collapse;font-size:12px;font-variant-numeric:tabular-nums;margin-top:6px}
th,td{text-align:left;padding:4px 6px;border-bottom:1px solid var(--grid);color:var(--ink2)}th{color:var(--muted);font-weight:600}
.tscroll{max-height:260px;overflow:auto;overscroll-behavior:contain;border-radius:8px}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.hidden{display:none !important}
@media (prefers-reduced-motion:reduce){*{transition:none !important}}
</style></head><body>
<div class="wrap">
  <div class="hd"><h1>💤 WakeReady</h1><span class="tag" id="mode">—</span></div>

  <nav class="filters" aria-label="밤 선택">
    <div class="nav">
      <button class="ib" id="prev" aria-label="이전 밤">‹</button>
      <select class="date" id="dateSel" aria-label="날짜 선택"></select>
      <button class="ib" id="next" aria-label="다음 밤">›</button>
    </div>
    <div class="seg" role="radiogroup" aria-label="데이터 출처">
      <button role="radio" aria-checked="false" data-src="cloud" id="srcCloud">☁️ 공식</button>
      <button role="radio" aria-checked="false" data-src="local" id="srcLocal">💍 로컬</button>
    </div>
  </nav>

  <section class="card" aria-label="수면 요약">
    <div class="hd"><span class="hero"><span id="hours">–</span><small id="target"></small></span><span class="live hidden" id="live">라이브</span></div>
    <div class="sub" id="sub">&nbsp;</div>
    <div class="meter" role="meter" aria-label="목표 수면 대비" id="meter"><i id="bar" style="width:0%"></i></div>
    <div class="qual" id="qual">&nbsp;</div>
    <div class="src" id="src">출처 확인 중…</div>
  </section>

  <section class="card" aria-label="수면 단계">
    <div class="ct"><span>수면 단계</span>
      <span class="legend"><span><i style="background:var(--wake)"></i>깸</span><span><i style="background:var(--rem)"></i>REM</span>
        <span><i style="background:var(--light)"></i>얕은</span><span><i style="background:var(--deep)"></i>깊은</span></span></div>
    <div class="chart" id="hypWrap"><svg id="hyp" tabindex="0" role="img" aria-label="5분 단위 수면 단계 차트"></svg><div class="tip" id="hypTip"></div></div>
    <div class="empty hidden" id="hypEmpty">이 밤에는 단계 데이터가 없어요.</div>
    <div class="grid" id="tiles" style="margin-top:14px"></div>
    <details class="tbl"><summary>표로 보기</summary><div class="tscroll"><table><thead><tr><th>시각</th><th>단계</th><th>심박</th><th>HRV</th></tr></thead><tbody id="tbody"></tbody></table></div></details>
  </section>

  <section class="card" id="hrCard" aria-label="심박수">
    <div class="ct"><span>심박수 <small style="color:var(--muted);font-weight:500">bpm · 5분</small></span><span id="hrRange" class="legend"></span></div>
    <div class="chart" id="hrWrap"><svg id="hr" tabindex="0" role="img" aria-label="5분 단위 심박수 차트"></svg><div class="tip" id="hrTip"></div></div>
    <div class="empty hidden" id="hrEmpty">심박 데이터가 없어요.</div>
  </section>

  <section id="liveBox">
    <div class="status" id="status" aria-live="polite">연결 대기 중…</div>
    <button class="btn" id="sync" onclick="doSync()">🔄 지금 동기화</button>
  </section>

  <div class="foot"><span id="meta"></span><span id="upd"></span></div>
</div>
<script>
const STAGE={WAKE:['깸','var(--wake)',0],REM:['REM','var(--rem)',1],LIGHT:['얕은','var(--light)',2],DEEP:['깊은','var(--deep)',3]};
const $=id=>document.getElementById(id);
const fmtDay=new Intl.DateTimeFormat('ko-KR',{month:'long',day:'numeric',weekday:'short'});
const fmtTime=new Intl.DateTimeFormat('ko-KR',{hour:'2-digit',minute:'2-digit',hour12:false});
const tstr=u=>fmtTime.format(new Date(u*1000));
let nights=[],st={},state={d:null,s:null},cur=null,curSrc=null;

function parseHash(){const p=new URLSearchParams(location.hash.slice(1));state.d=p.get('d');state.s=p.get('s');}
function writeHash(){const p=new URLSearchParams();if(state.d)p.set('d',state.d);if(state.s)p.set('s',state.s);history.replaceState(null,'','#'+p.toString());}
function pickNight(){if(!nights.length)return null;if(state.d){const n=nights.find(n=>n.day===state.d);if(n)return n;}return nights[0];}
function pickSrc(n){if(state.s&&n[state.s])return state.s;return n.cloud?'cloud':'local';}
function isLive(n){return n===nights[0];}
function eff(e){const a=e.rem_min+e.deep_min+e.light_min,t=a+e.awake_min;return t?Math.round(100*a/t):0;}
function quality(e){const ef=eff(e);
  if(e.rem_pct>=18&&e.deep_pct>=13&&ef>=85)return['😴 잘 잤어요','var(--good)'];
  if(e.rem_pct>=13&&e.deep_pct>=10&&ef>=78)return['🙂 양호','var(--accent)'];
  return['😐 뒤척임 많음','var(--warn)'];}

function renderFilters(){
  const sel=$('dateSel');sel.textContent='';
  nights.forEach(n=>{const o=document.createElement('option');o.value=n.day;
    o.textContent=fmtDay.format(new Date(n.day+'T12:00:00'));sel.appendChild(o);});
  if(cur){sel.value=cur.day;const i=nights.indexOf(cur);$('prev').disabled=i>=nights.length-1;$('next').disabled=i<=0;}
  for(const s of ['cloud','local']){const b=$(s==='cloud'?'srcCloud':'srcLocal');
    b.disabled=!(cur&&cur[s]);b.setAttribute('aria-checked',String(curSrc===s));}
}

function render(){
  cur=pickNight();if(!cur){$('src').textContent='아직 저장된 밤이 없어요. 링을 끼고 한 번 동기화해 보세요.';return;}
  curSrc=pickSrc(cur);const v=cur[curSrc];const live=isLive(cur);
  renderFilters();
  const T=st.target_hours||8;
  const h=(live&&st.hours!=null&&curSrc==='local'&&st.source==='local')?st.hours:v.total_sleep_hours;
  $('hours').textContent=h.toFixed(1);$('target').textContent=' / '+T+'h';
  $('bar').style.width=Math.min(100,100*h/T)+'%';$('meter').setAttribute('aria-valuenow',h.toFixed(1));
  const bs=v.bedtime_start?v.bedtime_start.slice(11,16):'',be=v.bedtime_end?v.bedtime_end.slice(11,16):'';
  $('sub').textContent=((live&&curSrc==='local'&&h<T)?('목표까지 '+(T-h).toFixed(1)+'h 남음 · '):'')+'취침 '+bs+' → '+(be||'…')+(v.efficiency!=null?' · 효율 '+Math.round(v.efficiency)+'%':'');
  const[ql,qc]=quality(v);$('qual').innerHTML='<span style="color:'+qc+'">'+ql+'</span>';
  $('live').classList.toggle('hidden',!live);
  const src=$('src');src.className='src'+(curSrc==='cloud'?' cloud':'');
  const other=cur[curSrc==='cloud'?'local':'cloud'];
  src.textContent='';const a=document.createElement('span');
  a.textContent=curSrc==='cloud'?('☁️ Oura 공식 기록 · '+cur.day):('💍 로컬 추정 · '+(v.method==='model'?'개인화 모델':'휴리스틱')+(live?' · 마지막 동기화 기준':''));
  src.appendChild(a);
  if(other){const s=document.createElement('small');s.textContent=(curSrc==='cloud'?'로컬 추정':'공식 기록')+' 있음 → 위에서 전환';src.appendChild(s);}
  renderTiles(v,other,curSrc);renderHyp(v);renderHR(v);renderTable(v);
  $('liveBox').classList.toggle('hidden',!live);
  $('meta').textContent=live?('상한 '+(st.cap_time||'')+(st.next_poll?' · 다음 '+st.next_poll:'')):'지난 밤 기록 · 실시간 아님';
  writeHash();
}

function renderTiles(v,other,srcName){
  const box=$('tiles');box.textContent='';const healthy=st.mode==='healthy';
  const rows=[['REM','rem_min','rem_pct',st.rem_min_target,25],['DEEP','deep_min','deep_pct',st.deep_min_target,20],['LIGHT','light_min','light_pct',null,60],['WAKE','awake_min',null,null,null]];
  for(const[k,mk,pk,tg,norm]of rows){
    const d=document.createElement('div');d.className='stat';
    const kk=document.createElement('div');kk.className='k';const sw=document.createElement('i');sw.style.background=STAGE[k][1];kk.appendChild(sw);
    kk.appendChild(document.createTextNode(k==='WAKE'?'깬 시간':STAGE[k][0]+(k==='REM'?' 수면':' 수면')));
    const vv=document.createElement('div');vv.className='v';vv.textContent=v[mk]+'분';
    const tt=document.createElement('div');tt.className='t';tt.textContent=pk?(healthy&&tg?('목표 '+tg+'분 · '+v[pk]+'%'):(v[pk]+'%')):'';
    const dd=document.createElement('div');dd.className='d';
    if(other){const same=Math.abs((other.end_unix||0)-(v.end_unix||0))<=1800;
      if(same){const diff=v[mk]-other[mk];dd.textContent=(diff>=0?'+':'')+diff+'분 vs '+(srcName==='cloud'?'로컬 추정':'공식 기록');}
      else dd.textContent=(srcName==='cloud'?'로컬은 ':'공식은 ')+(other.bedtime_end||'').slice(11,16)+'까지 기록';}
    d.append(kk,vv,tt,dd);
    if(norm){const m=document.createElement('div');m.className='mini';const i=document.createElement('i');i.style.background=STAGE[k][1];
      i.style.width=Math.min(100,100*(healthy&&tg?v[mk]/tg:v[pk]/norm))+'%';m.appendChild(i);d.appendChild(m);}
    box.appendChild(d);}
}

/* ── 히프노그램: 연속 구간을 한 막대로, 구간 사이 2px 표면 간격, 시간축 1시간 눈금, 크로스헤어 툴팁 ── */
const NS='http://www.w3.org/2000/svg';
function el(t,a){const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);return e;}
function hourTicks(eps,pw){/* 정각마다 눈금. 라벨(≈34px)이 겹치거나 오른쪽을 넘으면 생략. 반환: [소수 인덱스, 라벨] */
  if(!eps.length)return[];const start=eps[0].t-300,end=eps[eps.length-1].t,ew=pw/eps.length,out=[];let lastX=-1e9;
  const d=new Date(start*1000);d.setMinutes(0,0,0);d.setHours(d.getHours()+1);
  for(let h=Math.floor(d.getTime()/1000);h<=end;h+=3600){const fi=(h-start)/300,x=fi*ew;if(x-lastX<40||x>pw-30)continue;
    out.push([fi,tstr(h)]);lastX=x;}return out;}
let hypEps=[],hrEps=[];
function renderHyp(v){
  const eps=(v.epochs||[]).filter(e=>e.s);hypEps=eps;const svg=$('hyp');svg.textContent='';
  $('hypEmpty').classList.toggle('hidden',eps.length>0);$('hypWrap').classList.toggle('hidden',!eps.length);if(!eps.length)return;
  const W=svg.clientWidth||400,padL=34,padR=6,rowH=20,rows=4,axisH=18,H=rows*rowH+axisH+4;svg.setAttribute('viewBox','0 0 '+W+' '+H);svg.setAttribute('height',H);
  const pw=W-padL-padR,ew=pw/eps.length;
  Object.entries(STAGE).forEach(([k,[lab,col,r]])=>{svg.appendChild(el('line',{x1:padL,x2:W-padR,y1:r*rowH+rowH-1,y2:r*rowH+rowH-1,stroke:'var(--grid)','stroke-width':1}));
    const t=el('text',{x:padL-6,y:r*rowH+rowH/2+4,'text-anchor':'end',class:'ylab'});t.textContent=lab;svg.appendChild(t);});
  let i=0;while(i<eps.length){let j=i;while(j+1<eps.length&&eps[j+1].s===eps[i].s)j++;const r=STAGE[eps[i].s][2];
    const x=padL+i*ew,w=Math.max(1,(j-i+1)*ew-2);
    svg.appendChild(el('rect',{x:x+1,y:r*rowH+3,width:w,height:rowH-6,rx:3,fill:STAGE[eps[i].s][1]}));i=j+1;}
  for(const[i,lab]of hourTicks(eps,pw)){const x=padL+i*ew;svg.appendChild(el('line',{x1:x,x2:x,y1:0,y2:rows*rowH,stroke:'var(--grid)','stroke-width':1}));
    const t=el('text',{x:x+2,y:H-4,class:'axis'});t.textContent=lab;svg.appendChild(t);}
  const hair=el('line',{x1:0,x2:0,y1:0,y2:rows*rowH,stroke:'var(--ink2)','stroke-width':1,opacity:0,id:'hypHair'});svg.appendChild(hair);
  attachHover(svg,$('hypTip'),hair,padL,ew,eps.length,i=>{const e=eps[i];return [tstr(e.t)+' · '+STAGE[e.s][0]+(e.s==='WAKE'?'':' 수면'),(e.hr?'심박 '+Math.round(e.hr)+' bpm':'')+(e.rmssd?' · HRV '+Math.round(e.rmssd)+' ms':'')];});
}
function renderHR(v){
  const eps=(v.epochs||[]).filter(e=>e.hr>0);hrEps=eps;const svg=$('hr');svg.textContent='';
  $('hrEmpty').classList.toggle('hidden',eps.length>0);$('hrWrap').classList.toggle('hidden',!eps.length);$('hrRange').textContent='';if(!eps.length)return;
  const all=(v.epochs||[]);const W=svg.clientWidth||400,padL=34,padR=6,plotH=70,axisH=18,H=plotH+axisH+6;svg.setAttribute('viewBox','0 0 '+W+' '+H);svg.setAttribute('height',H);
  const pw=W-padL-padR,ew=pw/all.length;const hrs=eps.map(e=>e.hr);const lo=Math.floor(Math.min(...hrs)/5)*5,hi=Math.ceil(Math.max(...hrs)/5)*5;
  const y=h=>4+plotH-(h-lo)/(hi-lo||1)*plotH;
  for(const g of[lo,hi]){svg.appendChild(el('line',{x1:padL,x2:W-padR,y1:y(g),y2:y(g),stroke:'var(--grid)','stroke-width':1}));const t=el('text',{x:padL-6,y:y(g)+3,'text-anchor':'end',class:'axis'});t.textContent=g;svg.appendChild(t);}
  let d='',area='';all.forEach((e,i)=>{if(!(e.hr>0)){return;}const x=padL+i*ew+ew/2;d+=(d?'L':'M')+x.toFixed(1)+' '+y(e.hr).toFixed(1);});
  if(d){const first=d.slice(1).split('L')[0].split(' ')[0],lastX=d.split('L').pop().split(' ')[0];
    svg.appendChild(el('path',{d:d+'L'+lastX+' '+(4+plotH)+'L'+first+' '+(4+plotH)+'Z',fill:'var(--accent)',opacity:.1}));
    svg.appendChild(el('path',{d,fill:'none',stroke:'var(--accent)','stroke-width':2,'stroke-linejoin':'round','stroke-linecap':'round'}));}
  for(const[i,lab]of hourTicks(all,pw)){const x=padL+i*ew;const t=el('text',{x:x+2,y:H-4,class:'axis'});t.textContent=lab;svg.appendChild(t);}
  const mn=Math.min(...hrs),mx=Math.max(...hrs);$('hrRange').textContent='최저 '+Math.round(mn)+' · 최고 '+Math.round(mx)+' · 평균 '+Math.round(hrs.reduce((a,b)=>a+b,0)/hrs.length);
  const hair=el('line',{x1:0,x2:0,y1:0,y2:4+plotH,stroke:'var(--ink2)','stroke-width':1,opacity:0});svg.appendChild(hair);
  const dot=el('circle',{r:4,fill:'var(--accent)',stroke:'var(--surface)','stroke-width':2,opacity:0});svg.appendChild(dot);
  attachHover(svg,$('hrTip'),hair,padL,ew,all.length,i=>{const e=all[i];if(!(e.hr>0)){dot.setAttribute('opacity',0);return [tstr(e.t),'측정 없음'];}
    dot.setAttribute('cx',padL+i*ew+ew/2);dot.setAttribute('cy',y(e.hr));dot.setAttribute('opacity',1);
    return [tstr(e.t)+' · '+Math.round(e.hr)+' bpm',(e.rmssd?'HRV '+Math.round(e.rmssd)+' ms':'')+(e.s?' · '+STAGE[e.s][0]:'')];},dot);
}
function attachHover(svg,tip,hair,padL,ew,n,fmt,extra){
  let idx=-1;const wrap=svg.parentElement;
  function show(i){if(i<0||i>=n){hide();return;}idx=i;const x=padL+i*ew+ew/2;hair.setAttribute('x1',x);hair.setAttribute('x2',x);hair.setAttribute('opacity',1);
    const[a,b]=fmt(i);tip.textContent='';const bb=document.createElement('b');bb.textContent=a;tip.appendChild(bb);if(b){tip.appendChild(document.createElement('br'));tip.appendChild(document.createTextNode(b));}
    const r=svg.getBoundingClientRect(),px=x*r.width/(svg.viewBox.baseVal.width||r.width);tip.style.left=Math.max(70,Math.min(r.width-70,px))+'px';tip.style.top='-6px';tip.classList.add('on');}
  function hide(){idx=-1;hair.setAttribute('opacity',0);tip.classList.remove('on');if(extra)extra.setAttribute('opacity',0);}
  svg.onpointermove=ev=>{const r=svg.getBoundingClientRect();const vx=(ev.clientX-r.left)*(svg.viewBox.baseVal.width||r.width)/r.width;show(Math.floor((vx-padL)/ew));};
  svg.onpointerleave=hide;svg.onblur=hide;
  svg.onkeydown=ev=>{if(ev.key==='ArrowRight'){show(Math.min(n-1,idx+1));ev.preventDefault();}else if(ev.key==='ArrowLeft'){show(Math.max(0,idx-1));ev.preventDefault();}else if(ev.key==='Escape')hide();};
  svg.onfocus=()=>{if(idx<0)show(Math.floor(n/2));};
}
function renderTable(v){const tb=$('tbody');tb.textContent='';for(const e of(v.epochs||[])){const tr=document.createElement('tr');
  for(const c of[tstr(e.t),e.s?STAGE[e.s][0]:'–',e.hr?Math.round(e.hr):'–',e.rmssd?Math.round(e.rmssd):'–']){const td=document.createElement('td');td.textContent=c;tr.appendChild(td);}tb.appendChild(tr);}}

/* ── 데이터 로드: 밤 목록(30초) + 라이브 상태(5초) ── */
async function loadNights(){try{const r=await fetch('/api/nights?_='+Date.now());if(!r.ok)throw 0;nights=(await r.json()).nights||[];render();}catch(_){}}
async function tick(){
  try{const r=await fetch('/status.json?_='+Date.now());if(!r.ok)throw 0;st=await r.json();
    $('mode').textContent=st.mode==='healthy'?'건강 수면 모드':('총 '+(st.target_hours||8)+'h 모드');
    const stale=(st.status||'').indexOf('지난')>=0;let s=(stale?'⏸️ ':'')+(st.status||'…');
    if(st.phase==='syncing'){s+=' <span class="ok">(동기화 중…)</span>';if((st.fails||0)>=2)s+='<br><span class="off">링 연결 실패 '+st.fails+'회 — 링 착용·맥 근처·아이폰 BT OFF 확인</span>';}
    $('status').innerHTML=s;$('upd').textContent='갱신 '+(st.updated||'').slice(11,19);
    if(cur&&isLive(cur))render();
  }catch(_){$('status').innerHTML='<span class="off">⚠️ 세션 미실행 (tonight.sh 확인)</span>';}
}
async function doSync(){const b=$('sync');b.disabled=true;b.textContent='요청됨 — 곧 동기화…';try{await fetch('/sync',{method:'POST'});}catch(_){}
  setTimeout(()=>{b.disabled=false;b.textContent='🔄 지금 동기화';},8000);tick();setTimeout(loadNights,6000);}
$('dateSel').onchange=e=>{state.d=e.target.value;render();};
$('prev').onclick=()=>{const i=nights.indexOf(cur);if(i<nights.length-1){state.d=nights[i+1].day;render();}};
$('next').onclick=()=>{const i=nights.indexOf(cur);if(i>0){state.d=nights[i-1].day;render();}};
for(const b of document.querySelectorAll('.seg button'))b.onclick=()=>{state.s=b.dataset.src;render();};
window.addEventListener('hashchange',()=>{parseHash();render();});
window.addEventListener('resize',()=>{if(cur)render();});
parseHash();tick();loadNights();setInterval(tick,5000);setInterval(loadNights,30000);
</script></body></html>
"""


class H(BaseHTTPRequestHandler):
    def _send(self, body, ctype):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")   # 옛 페이지 캐시 방지
        self.send_header("Access-Control-Allow-Origin", "*")   # 로컬 개발/테스트 페이지에서 API 호출 허용
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/api/nights"):
            try:
                body = json.dumps({"nights": _se().nights_overview(str(DB)),
                                   "now": int(_time.time())}, ensure_ascii=False)
            except Exception:
                body = '{"nights":[]}'
            self._send(body, "application/json; charset=utf-8")
        elif self.path.startswith("/status.json"):
            try:
                self._send(status_payload(), "application/json; charset=utf-8")
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
