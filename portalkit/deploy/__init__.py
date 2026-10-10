"""Deploy safety: exposure classification, staging, ownership, replace/backup/rollback,
verification, status and the local dev server (ARCHITECTURE.md section 11).

The error classes live here so that every submodule (and the CLI) can map a refusal onto
the section 10 exit codes through one attribute, ``exit_code``:

====  ===================================================================
code  meaning
====  ===================================================================
1     check or verify failure (:class:`DeployError`, :class:`VerifyFailure`)
2     usage error (:class:`UsageError`)
3     exposure refusal: visibility mismatch, write through a symlink
      (:class:`ExposureRefusal`, :class:`SymlinkRefusal`)
4     ownership conflict (:class:`OwnershipConflict`)
5     blocked input: a build made with ``--allow-tmp-inputs`` (:class:`BlockedInput`)
====  ===================================================================

The public API is re-exported below; see ``docs/deploy.md`` for the runbook.
"""

from __future__ import annotations


class DeployError(RuntimeError):
    """Base class for every deploy refusal. ``exit_code`` follows section 10."""

    exit_code = 1

    def __init__(self, message: str, *, details: list[str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: list[str] = list(details or [])

    def __str__(self) -> str:
        if not self.details:
            return self.message
        return self.message + "\n" + "\n".join(f"  - {d}" for d in self.details)


class UsageError(DeployError):
    """Bad arguments: unknown portal id, missing legacy_dir for --replace, ... (exit 2)."""

    exit_code = 2


class ExposureRefusal(DeployError):
    """Visibility mismatch, unserved target or secure data fetched by a public page (exit 3)."""

    exit_code = 3


class SymlinkRefusal(ExposureRefusal):
    """A path component inside the target (or a legacy file) is a symlink (exit 3)."""


class OwnershipConflict(DeployError):
    """An existing file that portalkit does not own is in the way (exit 4)."""

    exit_code = 4

    def __init__(self, message: str, conflicts: list[str] | None = None) -> None:
        super().__init__(message, details=conflicts)
        self.conflicts: list[str] = list(conflicts or [])


class BlockedInput(DeployError):
    """The build was made with ``--allow-tmp-inputs`` or reads a tmp path (exit 5)."""

    exit_code = 5


class VerifyFailure(DeployError):
    """``verify`` found at least one failing check (exit 1)."""

    exit_code = 1


# Submodules import the error classes above from this package, so they are imported last.
from .exposure import (  # noqa: E402
    DEFAULT_ROOTS, URL_PREFIXES, Exposure, ExposureEntry, ExposureMap, PolicyDecision,
    check_references, check_target, classify, classify_url, scan,
)
from .stage import (  # noqa: E402
    DEFAULT_STAGE_ROOT, BUILD_ID_RE, build_provenance, check_stage_root, is_build_id,
    is_tmp_path, latest_build, list_builds, make_build_id, portal_stage_dir, prune_stage,
    read_build_report,
)
# The functions ``deploy.deploy`` and ``status.status`` are NOT re-exported under their own
# names: that would shadow the submodules ``portalkit.deploy.deploy`` and
# ``portalkit.deploy.status`` (``import portalkit.deploy.deploy as m`` would bind the
# function). Call them as ``from portalkit.deploy.deploy import deploy`` /
# ``from portalkit.deploy.status import status``, or via the aliases ``run_deploy`` and
# ``status_rows``.
from .deploy import (  # noqa: E402
    MANIFEST_NAME, DeployPlan, DeployResult, PublishResult, atomic_write, gc_target,
    is_shared_legacy_dir, load_manifest, plan_deploy, publish_assets, publish_immutable,
    resolve_target, select_portal, shared_legacy_dirs,
)
from .deploy import deploy as run_deploy  # noqa: E402
from .backup import (  # noqa: E402
    BACKUP_DIRNAME, RollbackResult, backup_files, read_backup, rollback, rollback_portal,
    stub_html,
)
from .verify import FileCheck, VerifyReport, verify, verify_http, verify_local  # noqa: E402
from .status import StatusRow, format_status, load_sites, portal_status  # noqa: E402
from .status import status as status_rows  # noqa: E402
from .htaccess import HTACCESS_TEXT, htaccess_owned, write_htaccess  # noqa: E402
from .devserver import DevServer, serve  # noqa: E402

__all__ = [
    "DeployError", "UsageError", "ExposureRefusal", "SymlinkRefusal", "OwnershipConflict",
    "BlockedInput", "VerifyFailure",
    "DEFAULT_ROOTS", "URL_PREFIXES", "Exposure", "ExposureEntry", "ExposureMap",
    "PolicyDecision", "check_references", "check_target", "classify", "classify_url", "scan",
    "DEFAULT_STAGE_ROOT", "BUILD_ID_RE", "build_provenance", "check_stage_root", "is_build_id",
    "is_tmp_path", "latest_build", "list_builds", "make_build_id", "portal_stage_dir",
    "prune_stage", "read_build_report",
    "MANIFEST_NAME", "DeployPlan", "DeployResult", "PublishResult", "atomic_write", "run_deploy",
    "gc_target", "is_shared_legacy_dir", "load_manifest", "plan_deploy", "publish_assets",
    "publish_immutable", "resolve_target", "select_portal", "shared_legacy_dirs",
    "BACKUP_DIRNAME", "RollbackResult", "backup_files", "read_backup", "rollback",
    "rollback_portal", "stub_html",
    "FileCheck", "VerifyReport", "verify", "verify_http", "verify_local",
    "StatusRow", "format_status", "load_sites", "portal_status", "status_rows",
    "HTACCESS_TEXT", "htaccess_owned", "write_htaccess",
    "DevServer", "serve",
]
