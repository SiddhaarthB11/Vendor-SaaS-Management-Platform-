import base64
import hashlib
import hmac
import os


PBKDF2_ITERATIONS = 260_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False

    try:
      scheme, iterations_text, salt_text, digest_text = password_hash.split("$", 3)
      if scheme != "pbkdf2_sha256":
          return False
      iterations = int(iterations_text)
      salt = base64.b64decode(salt_text.encode("ascii"))
      expected = base64.b64decode(digest_text.encode("ascii"))
    except (ValueError, TypeError):
      return False

    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)
