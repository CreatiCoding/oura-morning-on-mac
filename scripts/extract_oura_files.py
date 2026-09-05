#!/usr/bin/env python3
"""암호화 iPhone 백업에서 Oura 앱 컨테이너의 파일 목록/추출.

사용법:
  python3 extract_oura_files.py <backup_dir> <udid> <password> [--list | --dump <out_dir>]

- --list: Oura 도메인의 모든 파일 경로+크기 출력 (특히 .realm 위치 파악)
- --dump: Oura 도메인 파일 전체를 out_dir로 복원(원래 상대경로 유지)
"""
import sys
from pathlib import Path
from iOSbackup import iOSbackup

OURA_DOMAIN_HINT = "com.ouraring.oura"


def f_size(f):
    return f.get("size") or 0


def main():
    if len(sys.argv) < 5:
        print(__doc__)
        sys.exit(2)
    backup_dir, udid, password = sys.argv[1], sys.argv[2], sys.argv[3]
    mode = sys.argv[4]

    b = iOSbackup(udid=udid, cleartextpassword=password, backuproot=backup_dir)
    files = b.getBackupFilesList()  # list of dicts: name, domain, relativePath, size...

    oura = [f for f in files if OURA_DOMAIN_HINT in (f.get("domain") or "")]
    print(f"[i] Oura 도메인 파일 {len(oura)}개 발견", file=sys.stderr)

    if mode == "--list":
        for f in sorted(oura, key=lambda x: -(f_size(x))):
            print(f"{f_size(f):>12}  {f.get('domain')}  {f.get('relativePath')}")
        # .realm 강조
        realms = [f for f in oura if (f.get("relativePath") or "").endswith(".realm")]
        print(f"\n[i] .realm 파일 {len(realms)}개:", file=sys.stderr)
        for f in realms:
            print(f"  REALM: {f.get('relativePath')} ({f.get('size')} bytes)", file=sys.stderr)
        return

    if mode == "--dump":
        out_dir = Path(sys.argv[5])
        out_dir.mkdir(parents=True, exist_ok=True)
        for f in oura:
            rel = f.get("relativePath") or ""
            if not rel:
                continue
            dest = out_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                b.getFileDecryptedCopy(
                    relativePath=rel,
                    targetName=str(dest),
                    targetFolder=str(out_dir),
                )
            except Exception as e:
                # iOSbackup API 버전에 따라 시그니처가 다를 수 있어 폴백
                try:
                    data = b.getFileDecryptedData(relativePath=rel)
                    dest.write_bytes(data["data"] if isinstance(data, dict) else data)
                except Exception as e2:
                    print(f"[!] {rel} 추출 실패: {e} / {e2}", file=sys.stderr)
        print(f"[✓] Oura 파일을 {out_dir} 로 추출 완료", file=sys.stderr)
        return

    print(f"[!] 알 수 없는 모드: {mode}", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
