"""Password hashing (salted scrypt), login rate limiting and session secret.

Make a hash for config/factories.json:
    python -m app.auth "new-password"
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sys
import threading
import time
from pathlib import Path

N, R, P = 2 ** 14, 8, 1


def hash_password(pw: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=N, r=R, p=P, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, salt, dk = stored.split("$")
    except (AttributeError, ValueError):
        return False
    if algo != "scrypt":
        return False
    salt_b = base64.b64decode(salt)
    test = hashlib.scrypt(pw.encode(), salt=salt_b, n=N, r=R, p=P, dklen=32)
    return hmac.compare_digest(test, base64.b64decode(dk))


# a fixed dummy hash so unknown usernames cost the same time as known ones
DUMMY_HASH = hash_password("not-a-real-password", salt=b"\0" * 16)


class LoginLimiter:
    """Lock a username (and separately a client IP) for LOCK_S seconds after
    MAX_FAILS failed attempts inside WINDOW_S seconds."""
    MAX_FAILS, WINDOW_S, LOCK_S = 5, 300, 60

    def __init__(self):
        self._fails: dict[str, list[float]] = {}
        self._locked: dict[str, float] = {}
        self._lock = threading.Lock()

    def retry_after(self, *keys: str) -> int:
        now = time.monotonic()
        with self._lock:
            wait = max((self._locked.get(k, 0) - now for k in keys), default=0)
        return max(0, int(wait + 0.999))

    def fail(self, *keys: str):
        now = time.monotonic()
        with self._lock:
            for k in keys:
                f = [t for t in self._fails.get(k, []) if now - t < self.WINDOW_S] + [now]
                self._fails[k] = f
                if len(f) >= self.MAX_FAILS:
                    self._locked[k] = now + self.LOCK_S
                    self._fails[k] = []

    def success(self, *keys: str):
        with self._lock:
            for k in keys:
                self._fails.pop(k, None)
                self._locked.pop(k, None)


def session_secret(data_dir: Path) -> str:
    """SESSION_SECRET from the environment, else one generated once and kept in
    data/.session_secret so a restart does not log everyone out."""
    env = os.getenv("SESSION_SECRET")
    if env:
        return env
    f = data_dir / ".session_secret"
    if f.exists():
        return f.read_text().strip()
    data_dir.mkdir(parents=True, exist_ok=True)
    s = secrets.token_hex(32)
    f.write_text(s)
    return s


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit('usage: python -m app.auth "password"')
    print(hash_password(sys.argv[1]))
