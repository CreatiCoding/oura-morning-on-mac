#!/usr/bin/env python3
"""Vault(wakeready/credentials)에서 인증정보를 받아 .env + key.hex 재구성.

새 기기(맥미니)에서 클론 후 1회 실행하면 비밀 설정이 복구된다.
토큰: 환경변수 VAULT_TOKEN(+VAULT_ADDR) 우선, 없으면 ~/.claude/.mcp.json 의 vault 서버 args.

사용: python3 scripts/restore_from_vault.py
"""
import json
import os
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
KEY_FILE = ROOT / "key.hex"
DEFAULT_ADDR = "https://vault.creco.dev"
SECRET_PATH = "/v1/wakeready/data/credentials"


def get_token_addr():
    tok = os.environ.get("VAULT_TOKEN")
    addr = os.environ.get("VAULT_ADDR", DEFAULT_ADDR)
    if tok:
        return tok, addr
    # MCP 설정에서 추출
    for p in [Path.home() / ".claude" / ".mcp.json", Path.home() / ".claude.json"]:
        try:
            args = json.load(open(p))["mcpServers"]["vault"]["args"]
            tok = next(x.split("=", 1)[1] for x in args if x.startswith("VAULT_TOKEN="))
            a = next((x.split("=", 1)[1] for x in args if x.startswith("VAULT_ADDR=")), addr)
            return tok, a
        except Exception:
            continue
    return None, addr


def main():
    tok, addr = get_token_addr()
    if not tok:
        print("VAULT_TOKEN 을 찾을 수 없음. 환경변수로 주거나 ~/.claude/.mcp.json 에 vault 설정 필요.")
        raise SystemExit(1)
    req = urllib.request.Request(addr + SECRET_PATH, headers={"X-Vault-Token": tok})
    data = json.load(urllib.request.urlopen(req, timeout=15))["data"]["data"]

    # .env 병합 (기존 비밀번호가 아닌 설정은 보존)
    existing = {}
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1); existing[k.strip()] = v
    existing.update(data)
    ENV.write_text("\n".join(f"{k}={v}" for k, v in existing.items()) + "\n")
    os.chmod(ENV, 0o600)

    # key.hex 재생성 (oura CLI 가 --key-file 로 사용)
    if data.get("OURA_AUTH_KEY"):
        KEY_FILE.write_text(data["OURA_AUTH_KEY"].strip() + "\n")
        os.chmod(KEY_FILE, 0o600)

    print(f"[✓] .env 복구 ({len(data)} 비밀 + 기존 설정 병합), key.hex 재생성 완료")
    print("    이제 ./scripts/tonight.sh 실행 가능")


if __name__ == "__main__":
    main()
