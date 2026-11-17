"""Passwords, the last-way-in rule, and API keys made in Settings. Run: python tests/test_access.py"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["API_KEY"] = "env-key-0123456789abcdefghij"
import auth  # noqa: E402


def test_password_first():
    assert not auth.is_set() and auth.setup_code()
    try:
        auth.set_password("short")
    except ValueError:
        pass
    else:
        raise AssertionError("short password accepted")
    auth.set_password("correct horse battery")
    assert auth.is_set() and auth.has_password() and auth.setup_code() == ""
    assert auth.check_password("correct horse battery")
    assert not auth.check_password("correct horse batterY") and not auth.check_password("")
    cookie = auth.make_cookie().split(";")[0]
    assert auth.logged_in(cookie)
    # with no passkey, the password can't be removed
    assert auth.remove_password() != "" and auth.has_password()


def test_api_keys():
    assert auth.api_key_ok("Bearer env-key-0123456789abcdefghij")
    key = auth.create_api_key("Home Assistant")
    assert key.startswith("cs_") and auth.api_key_ok(f"Bearer {key}")
    listed = auth.api_keys()
    assert listed[0]["name"] == "Home Assistant" and listed[0]["prefix"] == key[:7]
    assert listed[0]["last_used"]                         # set by the check above
    assert key not in open(auth.AUTH_PATH).read()         # only the hash is stored
    assert not auth.api_key_ok(f"Bearer {key}x") and not auth.api_key_ok(f"Basic {key}")
    assert auth.revoke_api_key(listed[0]["id"]) == ""
    assert not auth.api_key_ok(f"Bearer {key}")
    assert auth.revoke_api_key("nope") != ""


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
