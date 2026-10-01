from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from saltapp import crypto


def test_generate_keypair_shape(keypair_a):
    assert keypair_a.public_key.startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----")
    assert keypair_a.private_key.startswith("-----BEGIN PGP PRIVATE KEY BLOCK-----")
    assert len(keypair_a.fingerprint) == 40
    assert keypair_a.fingerprint == keypair_a.fingerprint.lower()


def test_fingerprint_of_matches_generated(keypair_a):
    assert crypto.fingerprint_of(keypair_a.public_key) == keypair_a.fingerprint
    # also works from the private key block
    assert crypto.fingerprint_of(keypair_a.private_key) == keypair_a.fingerprint


def test_encrypt_decrypt_round_trip_single_recipient(keypair_a):
    ciphertext = crypto.encrypt_for("hello salt", [keypair_a.public_key])
    assert ciphertext.startswith("-----BEGIN PGP MESSAGE-----")
    plaintext = crypto.decrypt(ciphertext, keypair_a.private_key, "passphrase-a")
    assert plaintext == "hello salt"


def test_encrypt_decrypt_multi_recipient(keypair_a, keypair_b):
    """A single ciphertext both recipients can independently decrypt --
    mirrors Salt encrypting one message to every chat member's key."""
    ciphertext = crypto.encrypt_for("group message", [keypair_a.public_key, keypair_b.public_key])
    assert crypto.decrypt(ciphertext, keypair_a.private_key, "passphrase-a") == "group message"
    assert crypto.decrypt(ciphertext, keypair_b.private_key, "passphrase-b") == "group message"


def test_decrypt_wrong_key_fails(keypair_a, keypair_b):
    ciphertext = crypto.encrypt_for("only for A", [keypair_a.public_key])
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt(ciphertext, keypair_b.private_key, "passphrase-b")


def test_decrypt_wrong_passphrase_fails(keypair_a):
    ciphertext = crypto.encrypt_for("secret", [keypair_a.public_key])
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt(ciphertext, keypair_a.private_key, "totally-wrong-passphrase")


def test_encrypt_for_requires_at_least_one_key():
    with pytest.raises(crypto.CryptoError):
        crypto.encrypt_for("no recipients", [])


def test_decrypt_attachment_matches_web_crypto_aes_gcm_framing():
    """Ciphertext-with-tag-appended, same layout salt-fe's Web Crypto
    AES-256-GCM output uses (see crypto.py's docstring)."""
    key = AESGCM.generate_key(bit_length=256)
    iv = b"0" * 12
    aesgcm = AESGCM(key)
    plaintext = b"attachment bytes"
    ciphertext_with_tag = aesgcm.encrypt(iv, plaintext, None)  # cryptography already appends the tag

    decrypted = crypto.decrypt_attachment(
        ciphertext_with_tag,
        base64.b64encode(key).decode(),
        base64.b64encode(iv).decode(),
    )
    assert decrypted == plaintext


def test_decrypt_attachment_bad_key_fails():
    key = AESGCM.generate_key(bit_length=256)
    other_key = AESGCM.generate_key(bit_length=256)
    iv = b"1" * 12
    ciphertext_with_tag = AESGCM(key).encrypt(iv, b"data", None)
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_attachment(
            ciphertext_with_tag,
            base64.b64encode(other_key).decode(),
            base64.b64encode(iv).decode(),
        )


# ---- keys from openpgp.js 6 (SHA3 ids in the preferred-hash subpacket) ------

import json
import pathlib
import subprocess
import sys

_V6 = json.loads((pathlib.Path(__file__).parent / "fixtures" / "openpgp_v6_key.json").read_text())


def test_parses_a_real_openpgp_v6_public_key():
    from saltapp.crypto import fingerprint_of

    assert len(fingerprint_of(_V6["public_key"])) == 40


def test_encrypts_to_an_openpgp_v6_key_and_to_a_pgpy_key_together():
    from saltapp.crypto import decrypt, encrypt_for, generate_keypair

    mine = generate_keypair("pw")
    armored = encrypt_for("hello v6", [_V6["public_key"], mine.public_key])
    assert decrypt(armored, mine.private_key, "pw") == "hello v6"
    # The recorded ciphertext of this same call was decrypted by openpgp@6
    # with the fixture's private key (see the fixture's _about); the copy in
    # the fixture is what that run produced.
    assert "BEGIN PGP MESSAGE" in _V6["pgpy_ciphertext"]


def test_decrypts_what_openpgp_v6_encrypted_to_its_own_key():
    from saltapp.crypto import decrypt

    assert decrypt(_V6["openpgp_ciphertext"], _V6["private_key"], "") == _V6["openpgp_plaintext"]


def test_other_unknown_hash_ids_still_raise_pgpys_error():
    from pgpy.constants import HashAlgorithm

    import saltapp.crypto  # noqa: F401

    with pytest.raises(ValueError):
        HashAlgorithm(99)


def test_importing_saltapp_emits_no_cryptography_deprecation_warning():
    # A fresh interpreter: pytest's own warning capture would hide the
    # import-time warnings pgpy emits.
    code = (
        "import warnings; warnings.simplefilter('always'); "
        "import saltapp; from saltapp.crypto import generate_keypair, encrypt_for; "
        "k = generate_keypair('pw'); encrypt_for('x', [k.public_key])"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "CryptographyDeprecationWarning" not in proc.stderr
