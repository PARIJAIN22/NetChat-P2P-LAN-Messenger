"""
crypto.py - encryption of the TCP connections (needs the optional `cryptography` package).

NetChat invents NO cipher. It only combines standard, well-reviewed building blocks:

  X25519        Diffie-Hellman key exchange: both sides create a fresh ("ephemeral") key
                pair for every connection and end up with the same secret, which never
                travels over the network.  -> forward secrecy
  HKDF-SHA256   turns that secret into separate keys (one per direction + one for
                confirmation). The optional shared PASSPHRASE is mixed in here: with a
                different passphrase the two sides derive different keys.
  AES-256-GCM   encrypts every frame and detects any change to it (authenticated
                encryption). The nonce is a counter, so a replayed or re-ordered record fails.
  Ed25519       every user has a long-term identity key. It signs the ephemeral key, and
                SHA-256 of its public half is the FINGERPRINT two people can compare out loud.

The handshake (in the HI / AUTH frames, see protocol.py):

    dialer D                                   listener L
    HI{ident_D, eph_D, sig_D}  ------------->  check sig_D, derive keys from X25519(eph_L, eph_D)
                               <-------------  HI{ident_L, eph_L, sig_L, proof_L}
    check sig_L, derive the same keys,
    check proof_L  (fails if the passphrases differ)
    AUTH{proof_D}  ------------->              check proof_D
    ===== from now on every frame is an AES-GCM record in BOTH directions =====

What this does NOT give you (also in the README): without a passphrase nobody checks WHO owns
an identity key. A man in the middle can run two handshakes. The fingerprint comparison, or a
shared passphrase, is what defeats that.
"""

import hashlib
import hmac
import os
import struct

from .protocol import FrameError

try:
    from cryptography.exceptions import InvalidSignature, InvalidTag
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    AVAILABLE = True
except ImportError:                    # the program still works, just without encryption
    AVAILABLE = False

CONTEXT = b"NetChat-handshake-v1"       # signed and hashed into everything, so keys are only valid here


class CryptoError(FrameError):
    """The handshake or a record failed verification. (A FrameError, so a record that fails its
    authentication check is handled like any other bad frame: the connection is dropped.)"""


def fingerprint_of(public_bytes):
    """Short, readable hash of a public key: 'AB12 CD34 EF56 7890 1234' (80 of the 256 bits)."""
    digest = hashlib.sha256(public_bytes).hexdigest().upper()[:20]
    return " ".join(digest[i:i + 4] for i in range(0, 20, 4))


def verify(public_bytes, message, signature):
    """True if `signature` is a valid Ed25519 signature of `message` by `public_bytes`."""
    try:
        Ed25519PublicKey.from_public_bytes(public_bytes).verify(signature, message)
        return True
    except (InvalidSignature, ValueError):
        return False


def _raw_public(key):
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


class Identity:
    """My long-term Ed25519 key pair, kept in a file so the fingerprint stays the same."""

    def __init__(self, path=None):
        self.key = None
        if path and os.path.exists(path):
            try:
                with open(path, "rb") as f:
                    self.key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(f.read().decode().strip()))
            except (OSError, ValueError):
                self.key = None                    # unreadable file: make a new identity
        if self.key is None:
            self.key = Ed25519PrivateKey.generate()
            if path:
                raw = self.key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                             serialization.NoEncryption())
                with open(path, "wb") as f:
                    f.write(raw.hex().encode())
                try:
                    os.chmod(path, 0o600)           # private key: only the owner may read it
                except OSError:
                    pass
        self.public = _raw_public(self.key.public_key())
        self.fingerprint = fingerprint_of(self.public)

    def sign(self, data):
        return self.key.sign(data)


class SecureChannel:
    """Encrypts / decrypts one direction each. A record on the wire is
    [4-byte length][AES-GCM ciphertext + 16-byte tag] and wraps ONE plain frame."""

    def __init__(self, send_key, receive_key):
        self.sender = AESGCM(send_key)
        self.receiver = AESGCM(receive_key)
        self.send_counter = 0
        self.receive_counter = 0

    @staticmethod
    def _nonce(counter):
        # 96-bit nonce = 4 zero bytes + 64-bit counter. A key is used for one direction only
        # and the counter never repeats, which is what GCM requires.
        return b"\x00" * 4 + struct.pack(">Q", counter)

    def seal(self, plain):
        sealed = self.sender.encrypt(self._nonce(self.send_counter), plain, None)
        self.send_counter += 1
        return struct.pack(">I", len(sealed)) + sealed

    def open(self, sealed):
        try:
            plain = self.receiver.decrypt(self._nonce(self.receive_counter), sealed, None)
        except InvalidTag:
            raise CryptoError("a received record failed its authentication check (tampered, replayed or wrong key)")
        self.receive_counter += 1
        return plain


class Handshake:
    """One side of the key exchange. Create it with role 'dialer' or 'listener'."""

    def __init__(self, identity, passphrase, role):
        self.identity = identity
        self.role = role
        self.salt = hashlib.sha256(passphrase.encode("utf-8")).digest() if passphrase else b""
        self.eph = X25519PrivateKey.generate()
        self.eph_public = _raw_public(self.eph.public_key())
        self.remote_identity = None
        self.remote_eph = None
        self.keys = None

    # ---- helpers -------------------------------------------------------------------
    def _transcript(self, dialer_eph, listener_eph):
        return CONTEXT + dialer_eph + listener_eph

    def _parse(self, fields):
        try:
            return (bytes.fromhex(fields["ident"]), bytes.fromhex(fields["eph"]), bytes.fromhex(fields["sig"]))
        except (KeyError, TypeError, ValueError):
            raise CryptoError("the handshake is missing its key material")

    def _derive(self, dialer_eph, listener_eph, remote_eph):
        try:
            secret = self.eph.exchange(X25519PublicKey.from_public_bytes(remote_eph))
        except ValueError:
            raise CryptoError("bad ephemeral key")
        material = HKDF(algorithm=hashes.SHA256(), length=96, salt=self.salt or None,
                        info=self._transcript(dialer_eph, listener_eph)).derive(secret)
        self.keys = {"d2l": material[:32], "l2d": material[32:64], "confirm": material[64:]}

    def _proof(self, who, dialer_eph, listener_eph):
        return hmac.new(self.keys["confirm"], who + self._transcript(dialer_eph, listener_eph),
                        hashlib.sha256).hexdigest()

    def _verify_signature(self, ident, message, signature):
        if not verify(ident, message, signature):
            raise CryptoError("the peer's signature is wrong")

    # ---- dialer side ------------------------------------------------------------------
    def dialer_hello(self):
        """Fields for the dialer's HI frame."""
        signature = self.identity.sign(CONTEXT + b"D" + self.eph_public)
        return {"ident": self.identity.public.hex(), "eph": self.eph_public.hex(), "sig": signature.hex()}

    def dialer_finish(self, listener_fields):
        """Check the listener's HI. Returns the fields for the AUTH frame. Raises CryptoError."""
        ident, eph, sig = self._parse(listener_fields)
        self._verify_signature(ident, CONTEXT + b"L" + eph + self.eph_public, sig)
        self.remote_identity, self.remote_eph = ident, eph
        self._derive(self.eph_public, eph, eph)
        expected = self._proof(b"listener", self.eph_public, eph)
        if not hmac.compare_digest(str(listener_fields.get("proof", "")), expected):
            raise CryptoError("key confirmation failed: the shared passphrases differ (or the connection was tampered with)")
        return {"proof": self._proof(b"dialer", self.eph_public, eph)}

    # ---- listener side -------------------------------------------------------------------
    def listener_accept(self, dialer_fields):
        """Check the dialer's HI. Returns the fields for our answering HI. Raises CryptoError."""
        ident, eph, sig = self._parse(dialer_fields)
        self._verify_signature(ident, CONTEXT + b"D" + eph, sig)
        self.remote_identity, self.remote_eph = ident, eph
        self._derive(eph, self.eph_public, eph)
        signature = self.identity.sign(CONTEXT + b"L" + self.eph_public + eph)
        return {"ident": self.identity.public.hex(), "eph": self.eph_public.hex(), "sig": signature.hex(),
                "proof": self._proof(b"listener", eph, self.eph_public)}

    def listener_verify_auth(self, auth_fields):
        expected = self._proof(b"dialer", self.remote_eph, self.eph_public)
        if not hmac.compare_digest(str(auth_fields.get("proof", "")), expected):
            raise CryptoError("key confirmation failed: the shared passphrases differ")

    # ---- result ----------------------------------------------------------------------------
    def channel(self):
        if self.role == "dialer":
            return SecureChannel(self.keys["d2l"], self.keys["l2d"])
        return SecureChannel(self.keys["l2d"], self.keys["d2l"])

    @property
    def remote_fingerprint(self):
        return fingerprint_of(self.remote_identity)
