"""Who is this notebook running for?

Interactive sessions: the gateway forwards the user's PKI certificate DN to
the notebook backend in the ``x-user-dn`` header (``GALLERY_DN_HEADER``) —
on both the HTTP and WebSocket paths — and marimo exposes it through
``mo.app_meta().request``.

Scheduled runs (``marimo export``): there is no HTTP request, so the runner
sets ``GALLERY_USER_DN`` in the subprocess environment to the DN of whoever
created the schedule (or clicked "Run now").

Use it for on-behalf-of calls to external services and for usage logging:

    from gallery_shared import identity

    dn = identity.current_dn()      # full DN, or None outside the gallery
    who = identity.current_cn()     # display-friendly CN
"""

from __future__ import annotations

import os


def _header_name() -> str:
    return os.environ.get("GALLERY_DN_HEADER", "x-user-dn")


def current_dn() -> str | None:
    """The DN of the user this notebook execution is serving, if known."""
    try:
        import marimo as mo

        request = mo.app_meta().request  # None during `marimo export`
    except Exception:
        request = None
    if request is not None:
        dn = request.headers.get(_header_name())
        if dn:
            return dn
    return os.environ.get("GALLERY_USER_DN")


def current_cn() -> str | None:
    """The CN component of the current DN, for display; full DN if none."""
    dn = current_dn()
    if dn is None:
        return None
    for part in dn.split(","):
        key, _, value = part.strip().partition("=")
        if key.strip().upper() == "CN" and value.strip():
            return value.strip()
    return dn
