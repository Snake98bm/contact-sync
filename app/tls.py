"""HTTPS with the container's own certificate authority.

On first start it makes:
- a small private CA (10 years), limited by name constraints to this site's names, and
- a site certificate signed by it (2 years: Apple devices refuse ones longer than 825 days,
  even from a private CA). The site certificate renews itself from the CA when under 60 days are left,
  so the CA you installed keeps working for the full 10 years.

Install /ca.crt once on each device and the browser trusts the site with no warning, which
passkeys need. Names come from TLS_NAMES (comma-separated). Delete /data/tls/ and restart to
start over (then install the new CA again).
"""
import datetime as dt
import hashlib
import ipaddress
import logging
import os
import socket
import ssl
import subprocess
import tempfile

import store

log = logging.getLogger("tls")
DIR = os.path.join(store.DATA_DIR, "tls")
CA_CERT = os.path.join(DIR, "ca.crt")
CA_KEY = os.path.join(DIR, "ca.key")
CERT = os.path.join(DIR, "cert.pem")
KEY = os.path.join(DIR, "key.pem")
CA_DAYS = 3650
LEAF_DAYS = 730
RENEW_DAYS = 60

_ctx = None


def names():
    raw = os.environ.get("TLS_NAMES", "").strip()
    if raw:
        out = [n.strip().lower() for n in raw.split(",") if n.strip()]
    else:
        out = [(os.environ.get("DHCP_HOSTNAME", "").strip() or socket.gethostname()).lower()]
    for extra in ("localhost", "127.0.0.1"):
        if extra not in out:
            out.append(extra)
    return out


def canonical_host():
    """The one name the site is used under (passkeys are tied to it): PUBLIC_HOST, else the first
    TLS name with a dot in it, else the first TLS name."""
    env = os.environ.get("PUBLIC_HOST", "").strip().lower()
    if env:
        return env
    n = [x for x in names() if x not in ("localhost", "127.0.0.1")]
    dotted = [x for x in n if "." in x and not _is_ip(x)]
    return (dotted or n or ["localhost"])[0]


def _is_ip(n):
    try:
        ipaddress.ip_address(n)
        return True
    except ValueError:
        return False


def san(entries):
    return ",".join(f"IP:{n}" if _is_ip(n) else f"DNS:{n}" for n in entries)


def _constraints(entries):
    parts = []
    for n in entries:
        if _is_ip(n):
            parts.append(f"permitted;IP:{n}/255.255.255.255")
        else:
            parts.append(f"permitted;DNS:{n}")
    return ",".join(parts)


def _openssl(*args):
    subprocess.run(["openssl", *args], check=True, capture_output=True)


def _make_ca(n):
    _openssl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
             "-keyout", CA_KEY, "-out", CA_CERT, "-days", str(CA_DAYS),
             "-subj", f"/CN=contact-sync local CA ({n[0]})/O=contact-sync",
             "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
             "-addext", "keyUsage=critical,keyCertSign,cRLSign",
             "-addext", f"nameConstraints=critical,{_constraints(n)}")
    os.chmod(CA_KEY, 0o600)
    log.info("made a private CA for %s, valid %d days", ", ".join(n), CA_DAYS)


def _make_leaf(n):
    with tempfile.TemporaryDirectory(dir=DIR) as tmp:
        csr, ext = os.path.join(tmp, "leaf.csr"), os.path.join(tmp, "leaf.ext")
        key_new, cert_new = os.path.join(tmp, "key.pem"), os.path.join(tmp, "cert.pem")
        _openssl("req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                 "-keyout", key_new, "-out", csr, "-subj", f"/CN={n[0]}/O=contact-sync")
        with open(ext, "w") as f:
            f.write(f"subjectAltName={san(n)}\nextendedKeyUsage=serverAuth\n"
                    "keyUsage=critical,digitalSignature\nbasicConstraints=critical,CA:FALSE\n"
                    "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n")
        _openssl("x509", "-req", "-in", csr, "-CA", CA_CERT, "-CAkey", CA_KEY, "-CAcreateserial",
                 "-out", cert_new, "-days", str(LEAF_DAYS), "-sha256", "-extfile", ext)
        os.chmod(key_new, 0o600)
        # the chain the server sends: site cert then CA
        with open(cert_new, "a") as out, open(CA_CERT) as ca:
            out.write(ca.read())
        os.replace(key_new, KEY)
        os.replace(cert_new, CERT)
    log.info("made a site certificate for %s, valid %d days", ", ".join(n), LEAF_DAYS)


def _days_left(path):
    try:
        with open(path) as f:
            pem = f.read()
        out = subprocess.run(["openssl", "x509", "-noout", "-enddate"], input=pem, capture_output=True,
                             text=True, check=True).stdout
        end = dt.datetime.strptime(out.strip().split("=", 1)[1], "%b %d %H:%M:%S %Y %Z")
        return (end - dt.datetime.utcnow()).days
    except (OSError, ValueError, subprocess.CalledProcessError, IndexError):
        return -1


def ensure():
    """Make the CA and the site certificate if needed; renew the site certificate when it is
    close to expiring. Returns True if the site certificate changed."""
    os.makedirs(DIR, exist_ok=True)
    os.chmod(DIR, 0o700)
    n = names()
    if not (os.path.exists(CA_CERT) and os.path.exists(CA_KEY)):
        _make_ca(n)
        for p in (CERT, KEY):
            if os.path.exists(p):
                os.remove(p)
    if not (os.path.exists(CERT) and os.path.exists(KEY)) or _days_left(CERT) < RENEW_DAYS:
        _make_leaf(n)
        return True
    return False


def context():
    """The server's SSL context. Reloaded in place by refresh() after a renewal."""
    global _ctx
    if _ctx is None:
        _ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        _ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        _ctx.load_cert_chain(CERT, KEY)
    return _ctx


def refresh():
    """Called daily: renew the site certificate if due and load it for new connections."""
    if ensure() and _ctx is not None:
        _ctx.load_cert_chain(CERT, KEY)
        log.info("loaded the renewed site certificate")


def ca_pem():
    try:
        with open(CA_CERT, "rb") as f:
            return f.read()
    except OSError:
        return b""


def fingerprint(path=CA_CERT):
    """SHA-256 fingerprint of a certificate (the CA by default)."""
    try:
        with open(path) as f:
            der = ssl.PEM_cert_to_DER_cert(f.read())
    except (OSError, ValueError):
        return ""
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))
