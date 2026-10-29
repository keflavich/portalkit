"""The default Aladin Lite sources: CDS first, a UF-hosted copy as the fallback.

aladin.cds.unistra.fr went down on 2026-10-28 (connect hung ~25 s, then
refused).  ``sky/aladin.js`` tries ``cdn`` then ``mirror``, with
ALADIN_TIMEOUT_MS each, so a page keeps its sky view only if the mirror is
NOT on CDS.  The maintainer wants CDS to stay the default source.  The other
tests read ``config.ALADIN_CDN`` dynamically, so a mirror moved onto CDS, a
reordered pair, or a drift between the Python constants and the JS fallback
would pass them.
"""
import re
from pathlib import Path

from portalkit import config

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "web" / "js" / "sky" / "aladin.js"
CONSTANTS = ROOT / "web" / "js" / "core" / "constants.js"

_CDS_HOSTS = ("cds.unistra.fr", "u-strasbg.fr")


def _js_default(name):
    return re.search(rf"export const {name} = '([^']+)'", JS.read_text()).group(1)


def test_default_source_is_cds():
    assert config.ALADIN_CDN.startswith("https://aladin.cds.unistra.fr/")


def test_fallback_source_is_not_on_cds():
    """The fallback must survive the outage it exists for."""
    for url in (config.ALADIN_MIRROR, _js_default("DEFAULT_MIRROR")):
        assert url.startswith("https://") and url.endswith("/aladin.js"), url
        assert not any(host in url for host in _CDS_HOSTS), url


def test_js_and_python_defaults_agree():
    assert _js_default("DEFAULT_CDN") == config.ALADIN_CDN
    assert _js_default("DEFAULT_MIRROR") == config.ALADIN_MIRROR


def test_each_source_has_a_load_timeout():
    """A CDS-first order is only safe with a per-source timeout: a hanging
    connect fires no onerror until the browser's own ~30 s timeout."""
    ms = int(re.search(r"export const ALADIN_TIMEOUT_MS = (\d+);", CONSTANTS.read_text()).group(1))
    assert 0 < ms <= 10000
    assert "timeout" in re.search(r"export function loadAladin\(.*?\n\}", JS.read_text(), re.S).group(0)
