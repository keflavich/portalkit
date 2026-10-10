"""portalkit version and the hosted viewer-asset URL template.

The viewer assets are published once, immutably, per version (ARCHITECTURE.md section 0,
section 4.3 ``assets`` key). ``RES_BASE`` is the shared-resource base for templates,
filters, extinction and features (section 5.10).
"""

VERSION = "1.0.0"

#: Template for the asset base URL; format with ``version=``.
ASSET_BASE = "https://data.rc.ufl.edu/pub/adamginsburg/portalkit/v{version}/"

#: Template for the asset base when a portal is built with ``--vendor-assets``.
VENDOR_ASSET_BASE = "./_pk/v{version}/"

#: Shared resource base (``res`` key of page.json).
RES_BASE = "https://data.rc.ufl.edu/pub/adamginsburg/portalkit-res/"

#: Shared resource base when a portal is built with ``--vendor-assets``.
VENDOR_RES_BASE = "./res/"


def asset_base(version: str = VERSION, vendor: bool = False) -> str:
    """Return the asset base URL (always ending in ``/``) for ``version``."""
    return (VENDOR_ASSET_BASE if vendor else ASSET_BASE).format(version=version)


def res_base(vendor: bool = False) -> str:
    """Return the shared-resource base URL (always ending in ``/``)."""
    return VENDOR_RES_BASE if vendor else RES_BASE
