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

from dataclasses import dataclass

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


def bootstrap_admin_dns() -> set[str]:
    # Semicolon-separated: DNs themselves contain commas.
    return {dn.strip() for dn in settings.admin_dns.split(";") if dn.strip()}


@dataclass(frozen=True)
class Identity:
    dn: str
    name: str
    is_admin: bool
    groups: frozenset[str]


def identify(conn: HTTPConnection) -> Identity | None:
    """The requesting user with their groups, auto-provisioned on first sight.

    The user row is created once per DN per process (``app.state.known_dns``
    suppresses repeat writes); admin flag and group membership are read fresh
    every time so changes made in the admin UI apply immediately.
    """
    dn = current_user(conn)
    if dn is None:
        return None
    state = conn.app.state
    if dn not in state.known_dns:
        state.db.upsert_user(dn, cn_from_dn(dn), make_admin=dn in bootstrap_admin_dns())
        state.known_dns.add(dn)
    user = state.db.get_user_by_dn(dn)
    return Identity(
        dn=dn,
        name=user["display_name"] or cn_from_dn(dn),
        is_admin=user["is_admin"],
        groups=frozenset(state.db.groups_of(dn)),
    )
