"""Header-based PKI identity.

Production terminates TLS at a proxy (Nginx) that requires a client
certificate and passes its subject DN to the gateway in the ``x-user-dn``
header (``GALLERY_DN_HEADER``). The gateway trusts that header
unconditionally, so the proxy must be the only network path to the gateway
and must always *set* the header — never forward a client-supplied value.

There are no passwords and no sessions: identity is per-request. For local
development without the proxy, ``GALLERY_DEV_USER_DN`` supplies a fixed
identity used whenever the header is absent.
"""

from __future__ import annotations

from starlette.requests import HTTPConnection

from gallery.config import settings

MISSING_IDENTITY_HINT = (
    "login required: the request carried no user identity. In production the "
    "TLS-terminating proxy must send the client certificate DN in the "
    f"{settings.dn_header!r} header; for local development set "
    "GALLERY_DEV_USER_DN."
)


def current_user(conn: HTTPConnection) -> str | None:
    """The requesting user's DN (Request and WebSocket share the headers API)."""
    return conn.headers.get(settings.dn_header) or settings.dev_user_dn


def cn_from_dn(dn: str) -> str:
    """The CN component of a DN, for display; the full DN if there is none."""
    for part in dn.split(","):
        key, _, value = part.strip().partition("=")
        if key.strip().upper() == "CN" and value.strip():
            return value.strip()
    return dn
