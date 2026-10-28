"""The default Aladin Lite sources: self-hosted, and the same in Python and JS.

aladin.cds.unistra.fr went down on 2026-10-28, and every page whose first
source was the CDS CDN waited out its timeout before falling through.  The
other tests read ``config.ALADIN_CDN`` dynamically, so a revert to CDS, or a
drift between the Python constant and the JS fallback, would pass them.
"""
import re
from pathlib import Path

from portalkit import config

JS = Path(__file__).resolve().parents[2] / "web" / "js" / "sky" / "aladin.js"


def _js_default(name):
    return re.search(rf"export const {name} = '([^']+)'", JS.read_text()).group(1)


def test_default_aladin_chain_avoids_cds():
    """Neither default source may point at CDS."""
    for url in (config.ALADIN_CDN, config.ALADIN_MIRROR,
                _js_default("DEFAULT_CDN"), _js_default("DEFAULT_MIRROR")):
        assert "cds.unistra.fr" not in url and "u-strasbg.fr" not in url, url


def test_js_and_python_defaults_agree():
    assert _js_default("DEFAULT_CDN") == config.ALADIN_CDN
    assert _js_default("DEFAULT_MIRROR") == config.ALADIN_MIRROR
