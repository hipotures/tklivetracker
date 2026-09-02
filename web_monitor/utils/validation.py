from utils.username import normalize_tiktok_username


def sanitize_username(username):
    """Normalize a TikTok username before using it in paths or identity keys."""
    return normalize_tiktok_username(username)
