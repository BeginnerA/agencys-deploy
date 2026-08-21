"""SSL 证书探测：纯 HTTPS 连接获取证书到期信息（不依赖 SSH）"""
import socket
import ssl
import datetime


def probe_ssl(domain: str, port: int = 443, timeout: int = 10) -> dict:
    """探测域名 SSL 证书，返回证书信息和剩余天数

    Returns:
        dict: {cert_subject, cert_issuer, cert_expiry, days_left, error}
        - 成功时 error=None
        - 失败时 error=错误信息, 其余字段=None
    """
    try:
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(socket.socket(), server_hostname=domain) as s:
            s.settimeout(timeout)
            s.connect((domain, port))
            cert = s.getpeercert()
            # 解析到期时间: "Jun 15 12:00:00 2026 GMT"
            not_after = cert.get("notAfter", "")
            expiry_dt = datetime.datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z")
            days_left = (expiry_dt - datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)).days
            # 提取 CN
            subject = _extract_cn(cert.get("subject", ()))
            issuer = _extract_cn(cert.get("issuer", ()))
            return {
                "cert_subject": f"CN={subject}" if subject else "",
                "cert_issuer": f"CN={issuer}" if issuer else "",
                "cert_expiry": int(expiry_dt.timestamp()),
                "days_left": days_left,
                "error": None,
            }
    except Exception as e:
        return {
            "cert_subject": None,
            "cert_issuer": None,
            "cert_expiry": None,
            "days_left": None,
            "error": str(e),
        }


def _extract_cn(rdn_sequence) -> str:
    """从 X.509 RDN 序列中提取 commonName"""
    for rdn in rdn_sequence:
        for attr_type, attr_value in rdn:
            if attr_type == "commonName":
                return attr_value
    return ""
