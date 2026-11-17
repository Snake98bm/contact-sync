"""Passkeys (WebAuthn) on the standard library: a small CBOR reader, COSE keys, ES256 (P-256)
and RS256 signature checks, and the registration / login checks from the WebAuthn spec.
Attestation is not checked (attestation "none"): we only need to know the same authenticator
comes back, not who made it."""
import base64
import hashlib
import json
import struct


class Invalid(Exception):
    pass


def b64u(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def unb64u(s):
    s = s.strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ---------- CBOR (only what WebAuthn uses) ----------

def cbor_decode(data, i=0):
    """Return (value, next_offset)."""
    if i >= len(data):
        raise Invalid("CBOR ended early")
    ib = data[i]
    major, info = ib >> 5, ib & 31
    i += 1
    if info < 24:
        arg = info
    elif info in (24, 25, 26, 27):
        size = 1 << (info - 24)
        if i + size > len(data):
            raise Invalid("CBOR ended early")
        arg = int.from_bytes(data[i:i + size], "big")
        i += size
    else:
        raise Invalid("CBOR indefinite lengths are not supported")
    if major == 0:
        return arg, i
    if major == 1:
        return -1 - arg, i
    if major in (2, 3):
        if i + arg > len(data):
            raise Invalid("CBOR ended early")
        raw = data[i:i + arg]
        return (raw if major == 2 else raw.decode("utf-8")), i + arg
    if major == 4:
        out = []
        for _ in range(arg):
            v, i = cbor_decode(data, i)
            out.append(v)
        return out, i
    if major == 5:
        out = {}
        for _ in range(arg):
            k, i = cbor_decode(data, i)
            v, i = cbor_decode(data, i)
            out[k] = v
        return out, i
    if major == 6:                      # tag: ignore it, return the tagged value
        return cbor_decode(data, i)
    if major == 7:
        if info == 20:
            return False, i
        if info == 21:
            return True, i
        if info in (22, 23):
            return None, i
        if info == 26:
            return struct.unpack(">f", arg.to_bytes(4, "big"))[0], i
        if info == 27:
            return struct.unpack(">d", arg.to_bytes(8, "big"))[0], i
    raise Invalid("unsupported CBOR item")


# ---------- P-256 ECDSA ----------

P = 0xffffffff00000001000000000000000000000000ffffffffffffffffffffffff
A = P - 3
B = 0x5ac635d8aa3a93e7b3ebbd55769886bc651d06b0cc53b0f63bce3c3e27d2604b
G = (0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296,
     0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5)
N = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551


def _on_curve(pt):
    x, y = pt
    return 0 <= x < P and 0 <= y < P and (y * y - (x * x * x + A * x + B)) % P == 0


def _add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    (x1, y1), (x2, y2) = p1, p2
    if x1 == x2:
        if (y1 + y2) % P == 0:
            return None
        lam = (3 * x1 * x1 + A) * pow(2 * y1, -1, P) % P
    else:
        lam = (y2 - y1) * pow(x2 - x1, -1, P) % P
    x3 = (lam * lam - x1 - x2) % P
    return x3, (lam * (x1 - x3) - y1) % P


def _mul(k, pt):
    out = None
    while k:
        if k & 1:
            out = _add(out, pt)
        pt = _add(pt, pt)
        k >>= 1
    return out


def _der_sig(sig):
    """ECDSA signature DER SEQUENCE{r, s} -> (r, s)."""
    def read_int(b, i):
        if b[i] != 0x02:
            raise Invalid("bad signature")
        ln = b[i + 1]
        return int.from_bytes(b[i + 2:i + 2 + ln], "big"), i + 2 + ln
    if len(sig) < 8 or sig[0] != 0x30:
        raise Invalid("bad signature")
    i = 2 if sig[1] < 0x80 else 2 + (sig[1] & 0x7f)
    r, i = read_int(sig, i)
    s, _ = read_int(sig, i)
    return r, s


def verify_es256(x, y, message, sig):
    q = (x, y)
    if not _on_curve(q):
        raise Invalid("public key is not on P-256")
    r, s = _der_sig(sig)
    if not (1 <= r < N and 1 <= s < N):
        return False
    e = int.from_bytes(hashlib.sha256(message).digest(), "big")
    w = pow(s, -1, N)
    pt = _add(_mul(e * w % N, G), _mul(r * w % N, q))
    return pt is not None and pt[0] % N == r


# ---------- RSA PKCS#1 v1.5 SHA-256 ----------

_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def verify_rs256(n, e, message, sig):
    k = (n.bit_length() + 7) // 8
    if len(sig) != k or n.bit_length() < 2048:
        return False
    em = pow(int.from_bytes(sig, "big"), e, n).to_bytes(k, "big")
    t = _SHA256_PREFIX + hashlib.sha256(message).digest()
    return em == b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t


# ---------- COSE keys ----------

ES256, RS256 = -7, -257


def parse_cose(key):
    """COSE map -> stored key dict."""
    kty, alg = key.get(1), key.get(3)
    if kty == 2 and alg == ES256 and key.get(-1) == 1:
        x, y = int.from_bytes(key[-2], "big"), int.from_bytes(key[-3], "big")
        if not _on_curve((x, y)):
            raise Invalid("public key is not on P-256")
        return {"alg": ES256, "x": b64u(key[-2]), "y": b64u(key[-3])}
    if kty == 3 and alg == RS256:
        return {"alg": RS256, "n": b64u(key[-1]), "e": b64u(key[-2])}
    raise Invalid(f"unsupported key type (kty={kty}, alg={alg})")


def verify_sig(stored, message, sig):
    if stored["alg"] == ES256:
        return verify_es256(int.from_bytes(unb64u(stored["x"]), "big"),
                            int.from_bytes(unb64u(stored["y"]), "big"), message, sig)
    if stored["alg"] == RS256:
        return verify_rs256(int.from_bytes(unb64u(stored["n"]), "big"),
                            int.from_bytes(unb64u(stored["e"]), "big"), message, sig)
    return False


# ---------- WebAuthn ceremonies ----------

FLAG_UP, FLAG_UV, FLAG_AT = 0x01, 0x04, 0x40


def _client_data(raw, want_type, challenge, origins):
    try:
        cd = json.loads(raw)
    except ValueError:
        raise Invalid("bad clientDataJSON") from None
    if cd.get("type") != want_type:
        raise Invalid("wrong ceremony type")
    if cd.get("challenge") != challenge:
        raise Invalid("challenge doesn't match (old page? reload and try again)")
    if cd.get("origin") not in origins:
        raise Invalid(f"wrong origin {cd.get('origin')}")
    if cd.get("crossOrigin"):
        raise Invalid("cross-origin use is not allowed")


def _auth_data_head(ad, rp_id):
    if len(ad) < 37:
        raise Invalid("authenticator data too short")
    if ad[:32] != hashlib.sha256(rp_id.encode()).digest():
        raise Invalid("passkey is for a different site name")
    flags = ad[32]
    if not flags & FLAG_UP:
        raise Invalid("user presence missing")
    if not flags & FLAG_UV:
        raise Invalid("the device didn't verify you (PIN, fingerprint or face)")
    return flags, struct.unpack(">I", ad[33:37])[0]


def verify_registration(client_data_json, attestation_object, challenge, origins, rp_id):
    """Returns {"id", "key", "sign_count"} for a new passkey, or raises Invalid."""
    _client_data(client_data_json, "webauthn.create", challenge, origins)
    att, _ = cbor_decode(attestation_object)
    if not isinstance(att, dict) or "authData" not in att:
        raise Invalid("bad attestation object")
    ad = att["authData"]
    flags, count = _auth_data_head(ad, rp_id)
    if not flags & FLAG_AT:
        raise Invalid("no credential in the response")
    i = 37 + 16                                    # skip the AAGUID
    ln = struct.unpack(">H", ad[i:i + 2])[0]
    cred_id = ad[i + 2:i + 2 + ln]
    if not cred_id or len(cred_id) > 1023:
        raise Invalid("bad credential id")
    key, _ = cbor_decode(ad, i + 2 + ln)
    return {"id": b64u(cred_id), "key": parse_cose(key), "sign_count": count}


def verify_assertion(stored, client_data_json, authenticator_data, signature, challenge, origins, rp_id):
    """Check a login. Returns the new signature counter, or raises Invalid."""
    _client_data(client_data_json, "webauthn.get", challenge, origins)
    _, count = _auth_data_head(authenticator_data, rp_id)
    msg = authenticator_data + hashlib.sha256(client_data_json).digest()
    if not verify_sig(stored["key"], msg, signature):
        raise Invalid("signature check failed")
    old = stored.get("sign_count", 0)
    if count and old and count <= old:
        raise Invalid("this passkey's counter went backwards (possible copy); not accepted")
    return count
