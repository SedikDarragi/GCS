from main import hash_password
from main import is_password_hashed
from main import verify_password


def test_hash_format():
    h = hash_password("secret")
    algo, iterations, salt, digest = h.split("$")
    assert algo == "pbkdf2"
    assert int(iterations) > 0
    assert len(salt) == 32
    assert len(digest) == 64


def test_verify_correct_password():
    h = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", h)


def test_verify_wrong_password():
    h = hash_password("secret")
    assert not verify_password("wrong", h)


def test_hash_is_salted():
    assert hash_password("same") != hash_password("same")


def test_verify_empty_inputs():
    h = hash_password("x")
    assert not verify_password("", h)
    assert not verify_password("x", "")
    assert not verify_password("x", None)


def test_verify_malformed_stored():
    assert not verify_password("x", "garbage")
    assert not verify_password("x", "pbkdf2$abc")
    assert not verify_password("x", "$".join(["pbkdf2", "1000", "zz", "zz"]))


def test_legacy_plaintext_password():
    assert verify_password("admin123", "admin123")
    assert not verify_password("nope", "admin123")
    assert not is_password_hashed("admin123")


def test_is_password_hashed():
    assert is_password_hashed(hash_password("x"))
    assert not is_password_hashed("")
    assert not is_password_hashed(None)
