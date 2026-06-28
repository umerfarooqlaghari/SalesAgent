"""
Auth unit tests — no MongoDB required.
Run: python3 -m backend.tests.test_auth
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.auth.security import (
    create_access_token,
    decode_access_token,
    generate_api_key,
    hash_password,
    verify_password,
)


def test_password_hashing():
    hashed = hash_password("TestPass123!")
    assert hashed != "TestPass123!"
    assert verify_password("TestPass123!", hashed)
    assert not verify_password("wrong", hashed)
    print("✓ password hash + verify")


def test_jwt_roundtrip():
    token = create_access_token({"sub": "user-123", "email": "a@b.com", "role": "tenant_admin"})
    payload = decode_access_token(token)
    assert payload is not None
    assert payload["sub"] == "user-123"
    assert payload["email"] == "a@b.com"
    assert decode_access_token("invalid.token.here") is None
    print("✓ JWT create + decode")


def test_api_key_generation():
    k1 = generate_api_key()
    k2 = generate_api_key()
    assert k1 != k2
    assert len(k1) >= 32
    print("✓ API key generation")


def main():
    test_password_hashing()
    test_jwt_roundtrip()
    test_api_key_generation()
    print("\nAll auth unit tests passed.")


if __name__ == "__main__":
    main()
