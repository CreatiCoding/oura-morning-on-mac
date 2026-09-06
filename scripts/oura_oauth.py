#!/usr/bin/env python3
"""Oura OAuth2 토큰 자동 발급 (개인용). 로컬 콜백 서버로 코드→토큰 교환 후 .env 저장.

사용자가 할 일(1회):
  1) https://cloud.ouraring.com/oauth/applications 에서 앱 생성
     - Redirect URI 에 정확히:  http://localhost:8899/callback
     - Scopes: email, personal, daily 체크 (수면 데이터 포함)
  2) 발급된 client_id / client_secret 을 .env 에 넣기:
       OURA_CLIENT_ID=...
       OURA_CLIENT_SECRET=...
  3) python3 scripts/oura_oauth.py  실행 → 브라우저에서 '승인' 클릭

그러면 OURA_API_TOKEN(+refresh) 이 .env 에 저장되고, fetch_labels_api.py 가 바로 쓴다.
"""
import os
import sys
import json
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
REDIRECT = "http://localhost:8899/callback"
AUTH_URL = "https://cloud.ouraring.com/oauth/authorize"
TOKEN_URL = "https://api.ouraring.com/oauth/token"
SCOPES = "email personal daily"


def _env(key):
    if not ENV.exists():
        return ""
    for line in ENV.read_text().splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _save_env(updates):
    lines = ENV.read_text().splitlines() if ENV.exists() else []
    keys = set(updates)
    out = [l for l in lines if not any(l.startswith(k + "=") for k in keys)]
    out += [f"{k}={v}" for k, v in updates.items()]
    ENV.write_text("\n".join(out) + "\n")
    os.chmod(ENV, 0o600)


CID = _env("OURA_CLIENT_ID")
CSEC = _env("OURA_CLIENT_SECRET")
_result = {}


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.urlparse(self.path)
        if q.path != "/callback":
            self.send_response(404); self.end_headers(); return
        params = urllib.parse.parse_qs(q.query)
        code = params.get("code", [None])[0]
        _result["code"] = code
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers()
        msg = "인증 완료! 터미널로 돌아가세요." if code else "인증 실패(코드 없음)"
        self.wfile.write(f"<h2>{msg}</h2>".encode())

    def log_message(self, *a):
        pass


def exchange(code):
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": REDIRECT, "client_id": CID, "client_secret": CSEC,
    }).encode()
    with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=data), timeout=20) as r:
        return json.loads(r.read())


def main():
    if not CID or not CSEC:
        print("먼저 .env 에 OURA_CLIENT_ID / OURA_CLIENT_SECRET 를 넣으세요.")
        print("앱 생성: https://cloud.ouraring.com/oauth/applications")
        print(f"Redirect URI: {REDIRECT}")
        sys.exit(2)
    auth = AUTH_URL + "?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": CID,
        "redirect_uri": REDIRECT, "scope": SCOPES, "state": "wakeready",
    })
    print("브라우저에서 승인하세요 (안 열리면 아래 URL 직접 접속):")
    print(auth)
    try:
        webbrowser.open(auth)
    except Exception:
        pass
    srv = HTTPServer(("localhost", 8899), H)
    print("콜백 대기 중... (http://localhost:8899/callback)")
    while "code" not in _result:
        srv.handle_request()
    code = _result["code"]
    if not code:
        print("인증 코드 못 받음."); sys.exit(1)
    tok = exchange(code)
    at = tok.get("access_token"); rt = tok.get("refresh_token")
    if not at:
        print(f"토큰 교환 실패: {tok}"); sys.exit(1)
    _save_env({"OURA_API_TOKEN": at, "OURA_REFRESH_TOKEN": rt or ""})
    print("[✓] OURA_API_TOKEN 저장 완료 (.env). 이제:")
    print("    python3 scripts/fetch_labels_api.py")


if __name__ == "__main__":
    main()
