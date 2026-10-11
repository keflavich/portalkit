"""``python -m portalkit`` runs the builder CLI (same as the ``portalkit`` console script)."""

import sys

from .cli import main

sys.exit(main())
