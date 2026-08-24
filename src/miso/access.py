"""Cloudflare Access application-token validation at the origin."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.request import Request, urlopen


class AccessJWTError(ValueError):
    """Raised when a Cloudflare Access assertion cannot be trusted."""


JWKSLoader = Callable[[], object]
Clock = Callable[[], float]
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def _base64url_decode(value: str) -> bytes:
    if not value or len(value) > 16_384:
        raise AccessJWTError("Access assertion is malformed")
    try:
        return base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, TypeError) as error:
        raise AccessJWTError("Access assertion is malformed") from error


def _json_segment(value: str) -> Mapping[str, Any]:
    try:
        decoded = json.loads(_base64url_decode(value))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AccessJWTError("Access assertion is malformed") from error
    if not isinstance(decoded, dict):
        raise AccessJWTError("Access assertion is malformed")
    return decoded


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AccessJWTError(f"Access assertion {name} is invalid")
    try:
        integer = int(value)
    except (OverflowError, ValueError) as error:
        raise AccessJWTError(f"Access assertion {name} is invalid") from error
    if integer != value or integer < 0:
        raise AccessJWTError(f"Access assertion {name} is invalid")
    return integer


class AccessJWTVerifier:
    """Verify Cloudflare Access RS256 application tokens using rotating JWKs."""

    def __init__(
        self,
        team_domain: str,
        audience: str,
        *,
        jwks_loader: JWKSLoader | None = None,
        clock: Clock = time.time,
        cache_seconds: float = 3_600,
        leeway_seconds: int = 30,
    ) -> None:
        self.team_domain = team_domain.rstrip("/")
        self.audience = audience
        self._jwks_loader = jwks_loader or self._fetch_jwks
        self._clock = clock
        self._cache_seconds = cache_seconds
        self._leeway_seconds = leeway_seconds
        self._keys: dict[str, tuple[int, int]] = {}
        self._keys_expire_at = 0.0
        self._keys_refreshed_at = 0.0
        self._key_lock = threading.Lock()

    def verify(self, token: str) -> str:
        """Return the authenticated email after full assertion validation."""
        if not token or len(token) > 16_384:
            raise AccessJWTError("Access assertion is missing or too large")
        parts = token.split(".")
        if len(parts) != 3:
            raise AccessJWTError("Access assertion is malformed")
        header = _json_segment(parts[0])
        claims = _json_segment(parts[1])
        kid = header.get("kid")
        if (
            header.get("alg") != "RS256"
            or not isinstance(kid, str)
            or not 1 <= len(kid) <= 128
        ):
            raise AccessJWTError("Access assertion algorithm or key is invalid")

        key = self._key(kid)
        signature = _base64url_decode(parts[2])
        signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
        if not self._verify_rs256(signing_input, signature, key):
            raise AccessJWTError("Access assertion signature is invalid")

        self._validate_claims(claims)
        email = claims.get("email")
        if not isinstance(email, str) or not email.strip():
            raise AccessJWTError("Access assertion email is missing")
        return email

    def _validate_claims(self, claims: Mapping[str, Any]) -> None:
        if claims.get("iss") != self.team_domain:
            raise AccessJWTError("Access assertion issuer is invalid")
        if claims.get("type") != "app":
            raise AccessJWTError("Access assertion type is invalid")
        audience = claims.get("aud")
        audiences = [audience] if isinstance(audience, str) else audience
        if (
            not isinstance(audiences, list)
            or not all(isinstance(item, str) for item in audiences)
            or self.audience not in audiences
        ):
            raise AccessJWTError("Access assertion audience is invalid")

        now = int(self._clock())
        expires = _positive_integer(claims.get("exp"), "expiry")
        if now >= expires + self._leeway_seconds:
            raise AccessJWTError("Access assertion is expired")
        for claim, name in (("iat", "issued-at time"), ("nbf", "not-before time")):
            if claim in claims:
                value = _positive_integer(claims[claim], name)
                if value > now + self._leeway_seconds:
                    raise AccessJWTError("Access assertion is not valid yet")

    def _key(self, kid: str) -> tuple[int, int]:
        now = self._clock()
        with self._key_lock:
            expired = now >= self._keys_expire_at
            unknown_key_refresh_due = (
                kid not in self._keys
                and (not self._keys or now >= self._keys_refreshed_at + 60)
            )
            if expired or unknown_key_refresh_due:
                self._keys = self._parse_jwks(self._jwks_loader())
                self._keys_expire_at = now + self._cache_seconds
                self._keys_refreshed_at = now
            try:
                return self._keys[kid]
            except KeyError as error:
                raise AccessJWTError("Access assertion signing key is unknown") from error

    def _fetch_jwks(self) -> object:
        request = Request(
            f"{self.team_domain}/cdn-cgi/access/certs",
            headers={"Accept": "application/json", "User-Agent": "miso-access/1"},
        )
        try:
            with urlopen(request, timeout=5) as response:  # noqa: S310 - HTTPS validated
                body = response.read(262_145)
        except OSError as error:
            raise AccessJWTError("Access signing keys are unavailable") from error
        if len(body) > 262_144:
            raise AccessJWTError("Access signing-key response is too large")
        try:
            return json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AccessJWTError("Access signing-key response is invalid") from error

    @staticmethod
    def _parse_jwks(document: object) -> dict[str, tuple[int, int]]:
        if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
            raise AccessJWTError("Access signing-key response is invalid")
        keys: dict[str, tuple[int, int]] = {}
        for item in document["keys"][:16]:
            if not isinstance(item, dict):
                continue
            kid = item.get("kid")
            if (
                not isinstance(kid, str)
                or item.get("kty") != "RSA"
                or item.get("alg") not in {None, "RS256"}
                or item.get("use") not in {None, "sig"}
                or not isinstance(item.get("n"), str)
                or not isinstance(item.get("e"), str)
            ):
                continue
            try:
                modulus = int.from_bytes(_base64url_decode(item["n"]), "big")
                exponent = int.from_bytes(_base64url_decode(item["e"]), "big")
            except AccessJWTError:
                continue
            if modulus.bit_length() >= 512 and exponent >= 3 and exponent % 2 == 1:
                keys[kid] = (modulus, exponent)
        if not keys:
            raise AccessJWTError("Access signing-key response has no usable keys")
        return keys

    @staticmethod
    def _verify_rs256(
        signing_input: bytes, signature: bytes, key: tuple[int, int]
    ) -> bool:
        modulus, exponent = key
        key_bytes = (modulus.bit_length() + 7) // 8
        if len(signature) != key_bytes:
            return False
        signature_number = int.from_bytes(signature, "big")
        if signature_number >= modulus:
            return False
        encoded = pow(signature_number, exponent, modulus).to_bytes(key_bytes, "big")
        digest_info = _SHA256_DIGEST_INFO + hashlib.sha256(signing_input).digest()
        padding_length = key_bytes - len(digest_info) - 3
        if padding_length < 8:
            return False
        expected = b"\x00\x01" + b"\xff" * padding_length + b"\x00" + digest_info
        return hmac.compare_digest(encoded, expected)
