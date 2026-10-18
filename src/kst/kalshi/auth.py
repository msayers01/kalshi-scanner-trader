"""Kalshi request signing (docs/KALSHI_API_NOTES.md#authentication).

Signed message: f"{timestamp_ms}{METHOD}{path_without_query}", path starting at /trade-api/...
RSA keys: RSA-PSS(SHA-256, MGF1-SHA-256, salt = digest length). Ed25519 keys: plain signature.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa


class KalshiSigner:
    def __init__(self, key_id: str, private_key: rsa.RSAPrivateKey | ed25519.Ed25519PrivateKey) -> None:
        if not isinstance(private_key, rsa.RSAPrivateKey | ed25519.Ed25519PrivateKey):
            raise TypeError("Kalshi keys must be RSA or Ed25519")
        self.key_id = key_id
        self._key = private_key

    @classmethod
    def from_pem(cls, key_id: str, pem: bytes | str, password: bytes | None = None) -> KalshiSigner:
        data = pem.encode() if isinstance(pem, str) else pem
        key = serialization.load_pem_private_key(data, password=password)
        if not isinstance(key, rsa.RSAPrivateKey | ed25519.Ed25519PrivateKey):
            raise TypeError(f"unsupported key type {type(key).__name__}")
        return cls(key_id, key)

    @classmethod
    def from_file(cls, key_id: str, path: Path) -> KalshiSigner:
        return cls.from_pem(key_id, path.expanduser().read_bytes())

    @property
    def key_type(self) -> str:
        return "ed25519" if isinstance(self._key, ed25519.Ed25519PrivateKey) else "rsa"

    def sign(self, message: str) -> str:
        msg = message.encode("utf-8")
        if isinstance(self._key, ed25519.Ed25519PrivateKey):
            sig = self._key.sign(msg)
        else:
            sig = self._key.sign(
                msg,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                hashes.SHA256(),
            )
        return base64.b64encode(sig).decode("ascii")

    def headers(self, method: str, url_or_path: str, timestamp_ms: int | None = None) -> dict[str, str]:
        path = urlsplit(url_or_path).path if "://" in url_or_path else url_or_path.split("?", 1)[0]
        ts = str(timestamp_ms if timestamp_ms is not None else time.time_ns() // 1_000_000)
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": self.sign(ts + method.upper() + path),
        }
