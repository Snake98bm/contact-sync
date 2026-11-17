"""Passkey checks and the private CA, with a software authenticator whose keys and signatures come
from openssl. Needs the openssl CLI (in the image). Run: python tests/test_webauthn.py"""
import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
TMP = tempfile.mkdtemp()
os.environ["DATA_DIR"] = TMP
os.environ["TLS_NAMES"] = "contact-sync,contact-sync.home.example"
import auth  # noqa: E402
import tls  # noqa: E402
import webauthn as wa  # noqa: E402

RP = "contact-sync.home.example"
ORIGINS = {f"https://{RP}"}


# ---------- tiny CBOR encoder for building authenticator responses ----------

def cbor(v):
    def head(major, n):
        if n < 24:
            return bytes([major << 5 | n])
        for info, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
            if n < 1 << (8 * size):
                return bytes([major << 5 | info]) + n.to_bytes(size, "big")
    if isinstance(v, bool):
        return b"\xf5" if v else b"\xf4"
    if isinstance(v, int):
        return head(0, v) if v >= 0 else head(1, -1 - v)
    if isinstance(v, bytes):
        return head(2, len(v)) + v
    if isinstance(v, str):
        b = v.encode()
        return head(3, len(b)) + b
    if isinstance(v, list):
        return head(4, len(v)) + b"".join(cbor(x) for x in v)
    if isinstance(v, dict):
        return head(5, len(v)) + b"".join(cbor(k) + cbor(x) for k, x in v.items())
    raise TypeError(v)


def ossl(*args, data=None):
    return subprocess.run(["openssl", *args], input=data, capture_output=True, check=True).stdout


class SoftKey:
    def __init__(self, kind):
        self.kind = kind
        self.path = os.path.join(TMP, f"{kind}-{os.urandom(4).hex()}.pem")
        if kind == "ec":
            ossl("genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", self.path)
            point = ossl("pkey", "-in", self.path, "-pubout", "-outform", "DER")[-65:]
            assert point[0] == 4
            self.cose = {1: 2, 3: -7, -1: 1, -2: point[1:33], -3: point[33:]}
        else:
            ossl("genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", self.path)
            mod = ossl("rsa", "-in", self.path, "-noout", "-modulus").decode().strip().split("=")[1]
            self.cose = {1: 3, 3: -257, -1: bytes.fromhex(mod), -2: (65537).to_bytes(3, "big")}
        self.cred_id = os.urandom(32)
        self.count = 0

    def sign(self, msg):
        return ossl("dgst", "-sha256", "-sign", self.path, data=msg)

    def create(self, options, origin=f"https://{RP}", rp=RP, flags=0x45):
        cd = json.dumps({"type": "webauthn.create", "challenge": options["challenge"], "origin": origin}).encode()
        ad = (hashlib.sha256(rp.encode()).digest() + bytes([flags]) + struct.pack(">I", 0) + b"\0" * 16
              + struct.pack(">H", len(self.cred_id)) + self.cred_id + cbor(self.cose))
        return {"clientDataJSON": wa.b64u(cd), "attestationObject": wa.b64u(cbor({"fmt": "none", "attStmt": {}, "authData": ad}))}

    def get(self, options, origin=f"https://{RP}", rp=RP, flags=0x05, count=None, tamper=False):
        self.count = self.count + 1 if count is None else count
        cd = json.dumps({"type": "webauthn.get", "challenge": options["challenge"], "origin": origin}).encode()
        ad = hashlib.sha256(rp.encode()).digest() + bytes([flags]) + struct.pack(">I", self.count)
        sig = self.sign(ad + hashlib.sha256(cd).digest())
        if tamper:
            ad = ad[:-1] + bytes([ad[-1] ^ 1])
        return {"id": wa.b64u(self.cred_id), "clientDataJSON": wa.b64u(cd),
                "authenticatorData": wa.b64u(ad), "signature": wa.b64u(sig)}


def expect_invalid(fn, why):
    try:
        fn()
    except wa.Invalid:
        return
    raise AssertionError("accepted: " + why)


def test_cbor_roundtrip():
    v = {1: 2, -1: b"\x01\x02", "fmt": "none", "a": [1, -300, True], "big": 2 ** 40}
    assert wa.cbor_decode(cbor(v))[0] == v


def test_signatures_direct():
    for kind in ("ec", "rsa"):
        k = SoftKey(kind)
        stored = wa.parse_cose(k.cose)
        sig = k.sign(b"hello")
        assert wa.verify_sig(stored, b"hello", sig), kind
        assert not wa.verify_sig(stored, b"hellp", sig), kind


def test_setup_code_and_register_login():
    code = auth.setup_code()
    assert len(code) == 8 and not auth.is_set()
    assert not auth.check_setup_code("WRONG123")
    assert auth.check_setup_code(code.lower())               # case-insensitive

    ec, rsa = SoftKey("ec"), SoftKey("rsa")
    # wrong origin / wrong site name / no user verification are refused
    for kw, why in ((dict(origin="https://evil.example"), "wrong origin"), (dict(rp="evil.example"), "wrong rp"),
                    (dict(flags=0x41), "no UV")):
        opts = auth.register_options(RP, "test")
        expect_invalid(lambda: auth.finish_register(ec.create(opts, **kw), ORIGINS, RP, "x"), why)
    assert not auth.is_set()

    opts = auth.register_options(RP, "test")
    resp = ec.create(opts)
    auth.finish_register(resp, ORIGINS, RP, "PC")
    assert auth.is_set() and auth.setup_code() == ""
    # the same challenge can't be used twice
    expect_invalid(lambda: auth.finish_register(resp, ORIGINS, RP, "again"), "challenge reuse")
    auth.finish_register(rsa.create(auth.register_options(RP, "test")), ORIGINS, RP, "Hello")
    assert [c["name"] for c in auth.credentials()] == ["PC", "Hello"]

    # sign in with both
    for k, name in ((ec, "PC"), (rsa, "Hello")):
        assert auth.finish_login(k.get(auth.login_options(RP)), ORIGINS, RP) == name
    # tampered data, wrong origin, counter going backwards, reused challenge
    expect_invalid(lambda: auth.finish_login(ec.get(auth.login_options(RP), tamper=True), ORIGINS, RP), "tamper")
    expect_invalid(lambda: auth.finish_login(ec.get(auth.login_options(RP), origin="https://x.example"),
                                             ORIGINS, RP), "origin")
    expect_invalid(lambda: auth.finish_login(ec.get(auth.login_options(RP), count=1), ORIGINS, RP), "counter")
    opts = auth.login_options(RP)
    good = ec.get(opts)
    auth.finish_login(good, ORIGINS, RP)
    expect_invalid(lambda: auth.finish_login(good, ORIGINS, RP), "replay")
    # someone else's key with a known credential id
    thief = SoftKey("ec")
    thief.cred_id = ec.cred_id
    expect_invalid(lambda: auth.finish_login(thief.get(auth.login_options(RP)), ORIGINS, RP), "wrong key")

    # sessions
    cookie = auth.make_cookie(True).split(";")[0]
    assert auth.logged_in(cookie)
    auth.logout_everywhere()
    assert not auth.logged_in(cookie)

    # can't remove the last passkey
    ids = [c["id"] for c in auth.credentials()]
    assert auth.remove_credential(ids[0]) == ""
    assert auth.remove_credential(ids[1]) != ""


def test_private_ca():
    assert tls.ensure() is True
    assert tls.ensure() is False                       # nothing to renew
    ossl("verify", "-CAfile", tls.CA_CERT, tls.CERT)   # site cert chains to the CA (name constraints included)
    leaf = ossl("x509", "-in", tls.CERT, "-noout", "-text").decode()
    ca = ossl("x509", "-in", tls.CA_CERT, "-noout", "-text").decode()
    assert "DNS:contact-sync, DNS:contact-sync.home.example, DNS:localhost, IP Address:127.0.0.1" in leaf, leaf
    assert "CA:TRUE, pathlen:0" in ca and "Permitted" in ca
    assert 725 <= tls._days_left(tls.CERT) <= 730
    assert 3640 <= tls._days_left(tls.CA_CERT) <= 3650
    assert tls.canonical_host() == "contact-sync.home.example"
    assert len(tls.fingerprint().split(":")) == 32
    # a host outside the name constraints must not verify even if signed by the CA
    ext = os.path.join(TMP, "bad.ext")
    with open(ext, "w") as f:
        f.write("subjectAltName=DNS:www.bank.example\n")
    ossl("req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
         "-keyout", os.path.join(TMP, "bad.key"), "-out", os.path.join(TMP, "bad.csr"), "-subj", "/CN=bank")
    ossl("x509", "-req", "-in", os.path.join(TMP, "bad.csr"), "-CA", tls.CA_CERT, "-CAkey", tls.CA_KEY,
         "-CAcreateserial", "-out", os.path.join(TMP, "bad.pem"), "-days", "30", "-extfile", ext)
    try:
        ossl("verify", "-CAfile", tls.CA_CERT, os.path.join(TMP, "bad.pem"))
    except subprocess.CalledProcessError:
        pass
    else:
        raise AssertionError("name constraints not enforced")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
