import re


MIN_TIKTOK_USERNAME_LENGTH = 2
MAX_TIKTOK_USERNAME_LENGTH = 24
_TIKTOK_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._]+$")


def normalize_tiktok_username(value, *, strip_at=True):
    """Return a path-safe TikTok username or ``None`` when invalid."""
    if not isinstance(value, str) or not value:
        return None
    if value != value.strip():
        return None

    username = value[1:] if strip_at and value.startswith("@") else value
    if not (
        MIN_TIKTOK_USERNAME_LENGTH
        <= len(username)
        <= MAX_TIKTOK_USERNAME_LENGTH
    ):
        return None
    if username.endswith(".") or not _TIKTOK_USERNAME_PATTERN.fullmatch(username):
        return None
    return username
