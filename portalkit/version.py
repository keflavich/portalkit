"""portalkit version and the hosted viewer-asset URL template.

The viewer assets are published once, immutably, per version (ARCHITECTURE.md section 0,
section 4.3 ``assets`` key). ``RES_BASE`` is the shared-resource base for templates,
filters, extinction and features (section 5.10).

``ASSET_BASE`` and ``RES_BASE`` default to the author's own published host. Anyone else
publishing a non-vendored build overrides them with ``$PK_ASSET_BASE``/``$PK_RES_BASE``
(``PK_ASSET_BASE`` may contain ``{version}``, formatted the same way ``ASSET_BASE`` is) --
the module constants and their defaults are unchanged, so existing builds and the golden
tests (``tests/py/test_config.py``, ``tests/py/test_shell.py``) keep seeing the author's
URLs. A self-contained tree that needs no published host at all is simpler: build with
``--vendor-assets`` instead (README Quickstart).
"""

import os

VERSION = "1.0.0"

#: Template for the asset base URL; format with ``version=``. Default host; override with
#: ``$PK_ASSET_BASE`` (see :func:`asset_base`).
ASSET_BASE = "https://data.rc.ufl.edu/pub/adamginsburg/portalkit/v{version}/"

#: Template for the asset base when a portal is built with ``--vendor-assets``.
VENDOR_ASSET_BASE = "./_pk/v{version}/"

#: Shared resource base (``res`` key of page.json). Default host; override with
#: ``$PK_RES_BASE`` (see :func:`res_base`).
RES_BASE = "https://data.rc.ufl.edu/pub/adamginsburg/portalkit-res/"

#: Shared resource base when a portal is built with ``--vendor-assets``.
VENDOR_RES_BASE = "./res/"


def asset_base(version: str = VERSION, vendor: bool = False) -> str:
    """Return the asset base URL (always ending in ``/``) for ``version``.

    ``$PK_ASSET_BASE`` overrides :data:`ASSET_BASE` (ignored when ``vendor`` is set); it may
    contain ``{version}``, formatted exactly as :data:`ASSET_BASE` is.
    """
    if vendor:
        return VENDOR_ASSET_BASE.format(version=version)
    return os.environ.get("PK_ASSET_BASE", ASSET_BASE).format(version=version)


def res_base(vendor: bool = False) -> str:
    """Return the shared-resource base URL (always ending in ``/``).

    ``$PK_RES_BASE`` overrides :data:`RES_BASE` (ignored when ``vendor`` is set).
    """
    if vendor:
        return VENDOR_RES_BASE
    return os.environ.get("PK_RES_BASE", RES_BASE)
