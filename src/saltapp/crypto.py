# PGP message crypto, attachment decryption, and fingerprinting.
#
# Ported from salt-agent-sdk/src/crypto.ts (decrypt/encryptFor/generateKeypair/
# decryptAttachment) using pgpy instead of openpgp.js, and from
# salt-call-agent-example/pgp_bundle.py for the pgpy idioms (key.unlock as a
# context manager, PGPMessage.from_blob, etc). Wire-compatible with the TS
# SDK: same key shape (EdDSA/Ed25519 signing primary + ECDH/Curve25519
# encryption subkey -- what openpgp.js's `{type: "ecc", curve: "curve25519"}`
# produces, and what salt-fe's own generateKeys() uses), same multi-recipient
# PGP/MIME message format, same AES-256-GCM attachment framing.
from __future__ import annotations

import base64
from dataclasses import dataclass

import pgpy
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pgpy.constants import (
    CompressionAlgorithm,
    EllipticCurveOID,
    HashAlgorithm,
    KeyFlags,
    PubKeyAlgorithm,
    SymmetricKeyAlgorithm,
)

from saltapp.errors import SaltAppError


class CryptoError(SaltAppError):
    """A PGP operation (decrypt, encrypt, key parse) failed."""


@dataclass(frozen=True)
class GeneratedKeypair:
    public_key: str
    private_key: str
    fingerprint: str


def generate_keypair(passphrase: str) -> GeneratedKeypair:
    """Generate a fresh agent keypair: an EdDSA/Ed25519 signing primary key
    plus an ECDH/Curve25519 encryption subkey -- the same curve and key
    structure openpgp.js's `generateKey({type: "ecc", curve: "curve25519"})`
    produces (salt-agent-sdk's generateKeypair, and salt-fe's own key
    generation), so a key minted here is indistinguishable from one Salt's
    web client or the TS SDK would have made.
    """
    primary = pgpy.PGPKey.new(PubKeyAlgorithm.EdDSA, EllipticCurveOID.Ed25519)
    uid = pgpy.PGPUID.new("Salt Agent")
    primary.add_uid(
        uid,
        usage={KeyFlags.Certify, KeyFlags.Sign},
        hashes=[HashAlgorithm.SHA256],
        ciphers=[SymmetricKeyAlgorithm.AES256],
        # PGPMessage.new()'s own default compression is ZIP -- list it
        # first so a plain encrypt_for() call never mismatches this key's
        # stated preferences and warns.
        compression=[CompressionAlgorithm.ZIP, CompressionAlgorithm.ZLIB, CompressionAlgorithm.Uncompressed],
    )
    subkey = pgpy.PGPKey.new(PubKeyAlgorithm.ECDH, EllipticCurveOID.Curve25519)
    primary.add_subkey(subkey, usage={KeyFlags.EncryptCommunications, KeyFlags.EncryptStorage})

    fingerprint = str(primary.fingerprint).lower()

    if passphrase:
        primary.protect(passphrase, SymmetricKeyAlgorithm.AES256, HashAlgorithm.SHA256)

    return GeneratedKeypair(
        public_key=str(primary.pubkey),
        private_key=str(primary),
        fingerprint=fingerprint,
    )


def fingerprint_of(armored_public_key: str) -> str:
    """The lowercase hex fingerprint of an armored public (or private) key
    block -- matches openpgp.js's `getFingerprint()`, and what salt-api's
    contact-verification flow case-folds to on its own side (see the
    workspace CLAUDE.md's note on `normalizeFingerprint`)."""
    try:
        key, _ = pgpy.PGPKey.from_blob(armored_public_key)
    except Exception as exc:  # noqa: BLE001 -- any parse failure is a hard fail here
        raise CryptoError(f"not a valid PGP key: {exc}") from exc
    return str(key.fingerprint).lower()


def decrypt(armored_message: str, armored_private_key: str, passphrase: str) -> str:
    """Decrypt an armored PGP message with the agent's own private key.

    Salt encrypts each message to every chat member's public key, so the
    agent's own key is among the recipients and can read the ciphertext
    directly (see encrypt_for).
    """
    try:
        private_key, _ = pgpy.PGPKey.from_blob(armored_private_key)
        message = pgpy.PGPMessage.from_blob(armored_message)
    except Exception as exc:  # noqa: BLE001
        raise CryptoError(f"could not parse key or message: {exc}") from exc

    try:
        if private_key.is_protected:
            if not passphrase:
                raise CryptoError("private key is passphrase-protected but no passphrase was given")
            with private_key.unlock(passphrase):
                decrypted = private_key.decrypt(message)
        else:
            decrypted = private_key.decrypt(message)
    except CryptoError:
        raise
    except Exception as exc:  # noqa: BLE001 -- covers a ciphertext not addressed to this key
        raise CryptoError(f"decryption failed: {exc}") from exc

    plaintext = decrypted.message
    if isinstance(plaintext, (bytes, bytearray)):
        plaintext = plaintext.decode("utf-8")
    return plaintext


def encrypt_for(plaintext: str, armored_public_keys: list[str]) -> str:
    """Encrypt `plaintext` to one or more armored public keys, producing a
    single multi-recipient ciphertext every one of them can read -- what
    Salt stores in a message's `message` field.

    pgpy quietly generates a FRESH random session key on every `.encrypt()`
    call unless one is passed explicitly, even when handed an
    already-encrypted message -- so naively chaining `.encrypt()` calls (the
    way you'd read openpgp.js's API) produces a message whose extra
    recipients' session-key packets don't match the actual ciphertext at
    all. The session key must be generated once and threaded through every
    recipient's `.encrypt()` call.
    """
    if not armored_public_keys:
        raise CryptoError("encrypt_for requires at least one recipient public key")

    try:
        keys = [pgpy.PGPKey.from_blob(k)[0] for k in armored_public_keys]
    except Exception as exc:  # noqa: BLE001
        raise CryptoError(f"could not parse a recipient public key: {exc}") from exc

    message = pgpy.PGPMessage.new(plaintext)
    cipher = SymmetricKeyAlgorithm.AES256
    session_key = cipher.gen_key()

    encrypted: pgpy.PGPMessage | None = None
    try:
        for key in keys:
            pubkey = key if key.is_public else key.pubkey
            target = encrypted if encrypted is not None else message
            encrypted = pubkey.encrypt(target, sessionkey=session_key, cipher=cipher)
    except Exception as exc:  # noqa: BLE001
        raise CryptoError(f"encryption failed: {exc}") from exc

    assert encrypted is not None
    return str(encrypted)


def decrypt_attachment(ciphertext: bytes, key_b64: str, iv_b64: str) -> bytes:
    """Decrypt a message attachment's ciphertext bytes (see
    client.get_attachment). Mirrors salt-fe's src/utilities/attachments.js
    and salt-agent-sdk's decryptAttachment: Web Crypto AES-256-GCM output is
    ciphertext with the 16-byte auth tag appended, which is exactly the
    layout `cryptography`'s AESGCM.decrypt expects already -- no manual
    tag-splitting needed here (unlike the Node crypto module, which does
    require it).
    """
    key = base64.b64decode(key_b64)
    iv = base64.b64decode(iv_b64)
    try:
        return AESGCM(key).decrypt(iv, ciphertext, None)
    except Exception as exc:  # noqa: BLE001
        raise CryptoError(f"attachment decryption failed: {exc}") from exc
