from urllib.parse import urlsplit, urlunsplit


REDACTED = "[redacted]"


def redact_url_credentials(value):
    """Remove userinfo from a URL before it reaches logs."""
    if not isinstance(value, str):
        return value
    try:
        parsed = urlsplit(value)
        if parsed.username is None and parsed.password is None:
            return value
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, f"{REDACTED}@{host}", parsed.path, parsed.query, parsed.fragment))
    except (TypeError, ValueError):
        return REDACTED


def redact_secrets(message, *secrets):
    """Replace known non-empty secret values in diagnostic text."""
    result = str(message)
    for secret in secrets:
        if secret:
            result = result.replace(str(secret), REDACTED)
    return result
