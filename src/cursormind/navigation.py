"""Validate the optional page requested for a new browser's startup."""
from urllib.parse import urlsplit


def validate_start_url(url: str) -> str:
    url = url.strip()
    if not url:
        return ""
    try:
        parsed = urlsplit(url)
        valid = (
            len(url) <= 600 and not any(c.isspace() or ord(c) < 32 for c in url)
            and parsed.scheme in {"http", "https"} and bool(parsed.hostname)
            and parsed.username is None and parsed.password is None
        )
        parsed.port  # Reject malformed ports too.
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("start_url phải là URL http:// hoặc https:// hợp lệ, tối đa 600 ký tự.")
    return url


def resolve_start_url(goal: str, explicit: str = "") -> str:
    """Navigate only when explicitly supplied; mentioning a URL is not navigation."""
    return validate_start_url(explicit)
