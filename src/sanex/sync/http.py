"""Shared hardened HTTP URL and timeout construction."""

import httpx


DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 15.0
DEFAULT_WRITE_TIMEOUT = 15.0
DEFAULT_POOL_TIMEOUT = 5.0


def request_timeout() -> httpx.Timeout:
    """Return bounded timeouts shared by all sanea HTTP operations."""
    return httpx.Timeout(
        connect=DEFAULT_CONNECT_TIMEOUT,
        read=DEFAULT_READ_TIMEOUT,
        write=DEFAULT_WRITE_TIMEOUT,
        pool=DEFAULT_POOL_TIMEOUT,
    )


def https_origin_url(base_url: str, path: str) -> httpx.URL:
    """Append a fixed path to a strict credential-free HTTPS origin."""
    try:
        base = httpx.URL(base_url)

    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid sanea URL: {error}") from error

    if (
        base.scheme != "https"
        or not base.host
        or base.username
        or base.password
        or base.query
        or base.fragment
        or base.path not in ("", "/")
    ):
        raise ValueError(
            "sanea URL must be an absolute HTTPS origin without credentials, path, query or fragment"
        )

    return base.copy_with(path=path, query=None, fragment=None)
