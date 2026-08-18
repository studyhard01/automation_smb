"""외부 인증 package 없이 사용하는 비밀번호·session token 보안 함수."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets


PBKDF2_ITERATIONS = 600_000
_ALGORITHM = "pbkdf2_sha256"


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS, salt: bytes | None = None) -> str:
    """PBKDF2-HMAC-SHA256로 비밀번호를 단방향 hash한다."""

    actual_salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), actual_salt, iterations)
    encoded_salt = base64.urlsafe_b64encode(actual_salt).decode("ascii").rstrip("=")
    encoded_digest = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"{_ALGORITHM}${iterations}${encoded_salt}${encoded_digest}"


def verify_password(password: str, encoded: str) -> bool:
    """저장 형식을 검증하고 상수 시간 비교로 비밀번호를 확인한다."""

    try:
        algorithm, raw_iterations, raw_salt, expected = encoded.split("$", 3)
        if algorithm != _ALGORITHM:
            return False
        iterations = int(raw_iterations)
        if iterations < 1 or iterations > 10_000_000:
            return False
        salt = base64.urlsafe_b64decode(raw_salt + "=" * (-len(raw_salt) % 4))
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        actual = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(actual, expected)


def new_session_token() -> str:
    """브라우저에 한 번만 전달할 고엔트로피 session token을 만든다."""

    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    """DB에는 원문 session token 대신 SHA-256 digest만 저장한다."""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()
