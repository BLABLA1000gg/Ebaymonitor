from urllib.parse import urlsplit, urlunsplit


SUPPORTED_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}


def validate_proxy_url(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    value = value.strip()
    parsed = urlsplit(value)
    if parsed.scheme.casefold() not in SUPPORTED_PROXY_SCHEMES:
        raise ValueError("Proxy scheme must be http, https, socks5 or socks5h")
    if not parsed.hostname or parsed.port is None:
        raise ValueError("Proxy URL must contain a host and port")
    return value


def request_proxies(proxy_url: str | None) -> dict[str, str] | None:
    if not proxy_url:
        return None
    return {"http": proxy_url, "https": proxy_url}


def redact_proxy_url(proxy_url: str | None) -> str | None:
    if not proxy_url:
        return None
    parsed = urlsplit(proxy_url)
    hostname = parsed.hostname or ""
    if ":" in hostname:
        hostname = f"[{hostname}]"
    credentials = "***:***@" if parsed.username is not None else ""
    port = f":{parsed.port}" if parsed.port is not None else ""
    return urlunsplit((parsed.scheme, f"{credentials}{hostname}{port}", parsed.path, parsed.query, parsed.fragment))
