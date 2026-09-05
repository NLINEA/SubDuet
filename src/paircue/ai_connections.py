"""Validate AI destinations without ever including credentials in errors."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit, urlunsplit

PROVIDER_ORIGINS = {"openai": "https://api.openai.com", "zai": "https://api.z.ai"}
PROVIDERS = {*PROVIDER_ORIGINS, "custom", "local"}


def is_loopback_host(host: str) -> bool:
    host = host.rstrip(".").casefold()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def normalize_ai_url(value: str) -> str:
    """Reject ambiguous URLs before either urllib or an HTTP client normalizes them."""

    if not value or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValueError("AI endpoint must be an explicit URL without whitespace")
    if "\\" in value or "?" in value or "#" in value:
        raise ValueError("AI endpoint must not contain backslashes, query strings or fragments")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        port = parsed.port
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or "%" in host
        ):
            raise ValueError
        host = host.encode("idna").decode("ascii").lower()
        if parsed.scheme == "http" and not is_loopback_host(host):
            raise ValueError("AI provider URLs must use https unless the host is local loopback")
        authority = f"[{host}]" if ":" in host else host
        if port is not None and port != {"http": 80, "https": 443}[parsed.scheme]:
            authority += f":{port}"
        return urlunsplit((parsed.scheme, authority, parsed.path.rstrip("/"), "", ""))
    except (UnicodeError, ValueError) as exc:
        if str(exc).startswith("AI provider URLs"):
            raise
        raise ValueError(
            "AI endpoint must use http or https with a host and no credentials"
        ) from None


def ai_origin(value: str) -> str:
    parsed = urlsplit(normalize_ai_url(value))
    return f"{parsed.scheme}://{parsed.netloc}"


def validate_ai_connection(base_url: str, approved_origin: str, provider: str = "custom") -> str:
    """Fail closed if a configured destination no longer matches the user's approval."""

    if provider not in PROVIDERS:
        raise ValueError("choose an AI provider before entering its key")
    normalized = normalize_ai_url(base_url)
    origin = ai_origin(normalized)
    if provider in PROVIDER_ORIGINS and origin != PROVIDER_ORIGINS[provider]:
        raise ValueError("AI endpoint does not match the selected provider")
    if provider == "local" and not is_loopback_host(urlsplit(normalized).hostname or ""):
        raise ValueError("local AI must use a loopback host on this device")
    if not approved_origin:
        raise ValueError("confirm the AI destination and set APPROVED_ORIGIN before connecting")
    if normalize_ai_url(approved_origin) != ai_origin(approved_origin):
        raise ValueError("APPROVED_ORIGIN must contain only a scheme, host and optional port")
    if origin != ai_origin(approved_origin):
        raise ValueError("AI destination changed; confirm it again before using a key")
    return normalized
