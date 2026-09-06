"""터미널 QR 렌더 (있으면 qrcode 사용, 없으면 None). 의존성 없어도 앱은 동작."""
import io


def qr_ascii(text):
    """스캔 가능한 QR을 문자열로. qrcode 미설치 시 None."""
    try:
        import qrcode
    except Exception:
        return None
    try:
        qr = qrcode.QRCode(border=1, error_correction=qrcode.constants.ERROR_CORRECT_L)
        qr.add_data(text)
        qr.make(fit=True)
        buf = io.StringIO()
        qr.print_ascii(out=buf, invert=True)  # invert=밝은 배경 터미널에서도 스캔 잘 됨
        return buf.getvalue().rstrip("\n")
    except Exception:
        return None
