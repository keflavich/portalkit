"""portalkit.version: ``$PK_ASSET_BASE``/``$PK_RES_BASE`` overrides (README Install section).

The defaults are the author's published host and are asserted verbatim elsewhere
(tests/py/test_config.py, tests/py/test_shell.py); this file only checks the override path
and that the defaults are unchanged when the env vars are absent.
"""

from __future__ import annotations

from portalkit import version


def test_defaults_unchanged_without_env_override(monkeypatch):
    monkeypatch.delenv("PK_ASSET_BASE", raising=False)
    monkeypatch.delenv("PK_RES_BASE", raising=False)
    assert version.asset_base() == "https://data.rc.ufl.edu/pub/adamginsburg/portalkit/v1.0.0/"
    assert version.res_base() == "https://data.rc.ufl.edu/pub/adamginsburg/portalkit-res/"


def test_pk_asset_base_overrides_and_formats_version(monkeypatch):
    monkeypatch.setenv("PK_ASSET_BASE", "https://example.org/pk/v{version}/")
    assert version.asset_base("2.3.4") == "https://example.org/pk/v2.3.4/"
    assert version.asset_base() == f"https://example.org/pk/v{version.VERSION}/"


def test_pk_asset_base_without_a_version_placeholder_is_left_as_is(monkeypatch):
    monkeypatch.setenv("PK_ASSET_BASE", "https://example.org/pk/")
    assert version.asset_base("9.9.9") == "https://example.org/pk/"


def test_pk_res_base_overrides(monkeypatch):
    monkeypatch.setenv("PK_RES_BASE", "https://example.org/pk-res/")
    assert version.res_base() == "https://example.org/pk-res/"


def test_vendor_mode_ignores_both_env_overrides(monkeypatch):
    monkeypatch.setenv("PK_ASSET_BASE", "https://example.org/pk/v{version}/")
    monkeypatch.setenv("PK_RES_BASE", "https://example.org/pk-res/")
    assert version.asset_base("1.2.3", vendor=True) == "./_pk/v1.2.3/"
    assert version.res_base(vendor=True) == "./res/"


def test_module_constants_still_hold_the_authors_defaults():
    """The module-level constants themselves are never mutated by an env override -- only
    the asset_base()/res_base() functions read the environment."""
    assert version.ASSET_BASE == "https://data.rc.ufl.edu/pub/adamginsburg/portalkit/v{version}/"
    assert version.RES_BASE == "https://data.rc.ufl.edu/pub/adamginsburg/portalkit-res/"
