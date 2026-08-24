import base64
import hashlib
import json
import unittest

from miso.access import AccessJWTError, AccessJWTVerifier


TEST_MODULUS = int(
    "5c11e86bc71afad0ccb7b0b295278154dd796f393b60b596e3bf3cdf0351798"
    "0088cdd257e95204c25bd53b0c0a657234ee6610fde8e51de9ebe74427ef4601"
    "f6493e187e7f58dd06731a1c6a3cc339f4fae80784b995e042f883b68098b140b",
    16,
)
TEST_PRIVATE_EXPONENT = int(
    "1096a969c1d5fa9ae447b46e78b1457c24eb5c3ed393f923d6a4fe32b0465dbd"
    "d3cc8d8bd3e2ca8eedbdcef669bf8eb3769056b6d08ff622083d9d240cd4cb3c"
    "e7aa7bebaac70067c024fe99b97dce6a8a6a5c508ae4d7c17662ff3377901601",
    16,
)
TEST_EXPONENT = 65_537
DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _integer_base64url(value: int) -> str:
    return _base64url(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def _token(claims: dict[str, object]) -> str:
    header = _base64url(
        json.dumps({"alg": "RS256", "kid": "test-key", "typ": "JWT"}).encode()
    )
    payload = _base64url(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode()
    digest = DIGEST_INFO + hashlib.sha256(signing_input).digest()
    key_bytes = (TEST_MODULUS.bit_length() + 7) // 8
    encoded = b"\x00\x01" + b"\xff" * (key_bytes - len(digest) - 3) + b"\x00" + digest
    signature = pow(
        int.from_bytes(encoded, "big"), TEST_PRIVATE_EXPONENT, TEST_MODULUS
    ).to_bytes(key_bytes, "big")
    return f"{header}.{payload}.{_base64url(signature)}"


def _claims(**overrides: object) -> dict[str, object]:
    claims: dict[str, object] = {
        "aud": ["miso-application-audience"],
        "email": "Juan@Example.com",
        "exp": 2_000_000_600,
        "iat": 1_999_999_900,
        "nbf": 1_999_999_900,
        "iss": "https://sowe-tech.cloudflareaccess.com",
        "type": "app",
        "sub": "member-id",
    }
    claims.update(overrides)
    return claims


class AccessJWTVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.load_count = 0

        def load_jwks() -> object:
            self.load_count += 1
            return {
                "keys": [
                    {
                        "kid": "test-key",
                        "kty": "RSA",
                        "alg": "RS256",
                        "use": "sig",
                        "n": _integer_base64url(TEST_MODULUS),
                        "e": _integer_base64url(TEST_EXPONENT),
                    }
                ]
            }

        self.verifier = AccessJWTVerifier(
            "https://sowe-tech.cloudflareaccess.com",
            "miso-application-audience",
            jwks_loader=load_jwks,
            clock=lambda: 2_000_000_000,
        )

    def test_validates_signature_claims_and_caches_rotating_key_set(self) -> None:
        assertion = _token(_claims())
        self.assertEqual(self.verifier.verify(assertion), "Juan@Example.com")
        self.assertEqual(self.verifier.verify(assertion), "Juan@Example.com")
        self.assertEqual(self.load_count, 1)

        optional_time_claims = _claims()
        optional_time_claims.pop("iat")
        optional_time_claims.pop("nbf")
        self.assertEqual(
            self.verifier.verify(_token(optional_time_claims)), "Juan@Example.com"
        )

    def test_rejects_tampered_signature(self) -> None:
        assertion = _token(_claims())
        replacement = "A" if assertion[-1] != "A" else "B"
        with self.assertRaisesRegex(AccessJWTError, "signature"):
            self.verifier.verify(assertion[:-1] + replacement)

    def test_rejects_wrong_audience_and_expired_assertion(self) -> None:
        with self.assertRaisesRegex(AccessJWTError, "audience"):
            self.verifier.verify(_token(_claims(aud=["another-application"])))
        with self.assertRaisesRegex(AccessJWTError, "expired"):
            self.verifier.verify(_token(_claims(exp=1_999_999_000)))

    def test_rejects_service_token_without_member_email(self) -> None:
        claims = _claims(type="app")
        claims.pop("email")
        with self.assertRaisesRegex(AccessJWTError, "email"):
            self.verifier.verify(_token(claims))


if __name__ == "__main__":
    unittest.main()
