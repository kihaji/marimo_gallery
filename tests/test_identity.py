"""gallery_shared.identity and the proxy's DN header injection."""

from types import SimpleNamespace

from gallery.config import settings
from gallery.proxy import _set_identity_header
from gallery_shared import identity

DN = "CN=Alice,OU=Eng,O=Example,C=US"


def test_current_dn_env_fallback(monkeypatch):
    # Outside a marimo request context (as in `marimo export`), the env wins.
    monkeypatch.setenv("GALLERY_USER_DN", DN)
    assert identity.current_dn() == DN
    assert identity.current_cn() == "Alice"


def test_current_dn_absent(monkeypatch):
    monkeypatch.delenv("GALLERY_USER_DN", raising=False)
    assert identity.current_dn() is None
    assert identity.current_cn() is None


def test_proxy_sets_identity_header_from_request():
    conn = SimpleNamespace(headers={settings.dn_header: DN})
    headers = {settings.dn_header: "spoofed-upstream-value"}
    _set_identity_header(headers, conn)
    assert headers[settings.dn_header] == DN


def test_proxy_injects_dev_dn_and_strips_when_anonymous(monkeypatch):
    monkeypatch.setattr(settings, "dev_user_dn", DN)
    headers: dict[str, str] = {}
    _set_identity_header(headers, SimpleNamespace(headers={}))
    assert headers[settings.dn_header] == DN

    monkeypatch.setattr(settings, "dev_user_dn", None)
    headers = {settings.dn_header: "left-over"}
    _set_identity_header(headers, SimpleNamespace(headers={}))
    assert settings.dn_header not in headers
