# Deploying a portal

This runbook covers the path from a staged build to a served portal: build, review, deploy,
verify, replace legacy pages, and roll back. The code lives in `portalkit/deploy/`; the
contract is ARCHITECTURE.md section 11 (with the overrides in IMPLEMENTATION_NOTES.md).

## Overview

```
portalkit build FAMILY                     # writes <stage_root>/<family>/<build_id>/<portal>/
portalkit serve BUILD_DIR [--auth u:p]     # review locally (ssh tunnel or Playwright)
portalkit deploy FAMILY BUILD_ID --dry-run # plan, exposure and ownership checks; writes nothing
portalkit deploy FAMILY BUILD_ID           # write into the target, record ownership
portalkit verify FAMILY [--http]           # local sha256 checks; HTTP status/auth/sha/headers
portalkit deploy FAMILY BUILD_ID --replace # turn legacy pages into redirect stubs (backed up)
portalkit rollback FAMILY BUILD_ID         # restore the legacy pages from the backup
portalkit status [FAMILY] [--json]         # staged vs served, drift, legacy changes
portalkit prune-stage FAMILY [--keep 5]    # remove old staged builds
```

Exit codes: `1` verify or check failure, `2` usage error, `3` exposure or symlink refusal,
`4` ownership conflict, `5` blocked tmp input.

## 1. Build and stage

* A build id is `YYYYMMDDTHHMMSSZ-<8 hex>`; the hex digits hash the adapter sha and the
  input records, so the same inputs give the same suffix (`stage.make_build_id`).
* The stage root is `--stage-root`, else `$PK_STAGE_ROOT`, else
  `/orange/adamginsburg/portalkit_stage`. `stage.check_stage_root` refuses (exit 3) a stage
  root that the web server can reach.
* `build_report.json` sits next to `portal.json` in the staged portal (or in the build
  directory). Deploy reads it for provenance and never copies it into the target. Deploy
  looks for these keys at the top level or under `provenance`:
  `adapter_sha`, `legacy_modules` (`[{name, path, sha256}]`), `portalkit`,
  `allow_tmp_inputs`, `inputs` (`[{path, sha256, ...}]` or paths).
* A build made with `--allow-tmp-inputs`, or one whose inputs lie under `/blue/*/tmp/`,
  `/tmp/` or `*/scratchpad/`, can be served for review and is refused by deploy (exit 5).

## 2. Review with the development server

```
portalkit serve /orange/adamginsburg/portalkit_stage/FAMILY/BUILD_ID/PORTAL --port 8765
python -m portalkit.deploy.devserver BUILD_DIR --auth user:pw --content-encoding gzip --no-range
```

`DevServer` (also used by the e2e tests) emulates the production host:

| Option | Behaviour |
|---|---|
| `auth="user:pass"` | 401 with `WWW-Authenticate: Basic` until the credentials match |
| `range=True` / `--no-range` | single-range 206/416, suffix ranges; with range off, `Accept-Ranges: none` and full bodies |
| `content_encoding="auto"` | `*.gz` sent as stored with `Content-Encoding: gzip` and the inner type |
| `content_encoding="none"` | never a `Content-Encoding`; `*.gz` is `application/gzip` |
| `content_encoding="gzip"` | compress every body when the client accepts gzip, `.bin` included (mod_deflate); Range is then ignored |
| `latency_ms` | fixed delay before each response |
| `cache_control` | a header value, or `"htaccess"` to apply the section 11.5 rules |

Every request is logged in `DevServer.requests` (method, path, status, bytes, range,
encoding, auth). `.ht*` paths and `..` components answer 403.

## 3. Exposure check

`exposure.scan()` walks `/orange/adamginsburg/web/public` and `.../web/secure`, follows
directory symlinks to depth 4, prunes `*_hips`, and maps each reachable real directory to its
URL prefix (`https://data.rc.ufl.edu/pub/adamginsburg/` and
`https://data.rc.ufl.edu/secure/adamginsburg/`). `classify(path)` returns `public`, `secure`,
`both` or `unserved`, with the URLs and the symlink chains that expose the path.

| Portal | Target | Result |
|---|---|---|
| secure | secure | ok |
| secure | public or both | refused (exit 3), the public symlink chain is printed |
| public | public or both | ok (`both` prints a notice) |
| public | secure only | refused unless `served_by = "external"` or `--yes`; a warning either way |
| any | unserved | refused unless `served_by = "external"` |

References in the staged pages are checked too. A public page that fetches a secure URL (a
dataset, image, script or a relative path that resolves into a secure-only directory) is
refused (exit 3). Plain links to secure pages (`<a href>`, `links`/`related` entries, objects
with `link_only: true` or an `auth` key) are allowed with a "login required" warning.

The full scan of the real roots reads about 860k directory entries and took about 12 minutes
on the shared filesystem (most of it in a few very large directories under
`web/secure/salt/` and `web/secure/ACES/`). The result is cached for an hour in
`$PK_CACHE_DIR` (default `~/.cache/portalkit/exposure-<key>.json`); `scan(cache_s=0)` forces a
fresh walk.

## 4. Deploy

```
portalkit deploy FAMILY BUILD_ID [--portal ID] [--target DIR] [--dry-run] [--yes] [--htaccess]
```

The target is `target_dir`, else `<legacy_dir>/unified`. Deploy refuses in this order:
blocked inputs (5), exposure (3), symlinks (3), ownership (4). A dry run applies the same
checks and prints the plan.

Write rules:

* Each file goes to `<name>.pk-tmp` (opened with `O_EXCL | O_NOFOLLOW`), is fsynced and moved
  with `os.replace`; the directory is fsynced afterwards.
* `os.lstat` runs on the target and every path component; any symlink refuses.
* Order: content-hashed data, then stable-name documents (`manifest.json`, `index.json`,
  `images.json`, ...), then `portal.json`, then HTML. A viewer that loads during a deploy
  sees either the old page or a page whose data is already in place.
* Only paths listed in this portal's ownership manifest, or absent paths, are written. Any
  other existing file is a conflict (exit 4) and is left untouched.
* Superseded content-hashed files stay until `deploy --gc` (`deploy.gc_target`) removes the
  unreferenced ones older than 7 days.

### Ownership manifest

`<target>/.portalkit-deploy.json` (schema `pk-deploy/1`, validated with
`schema.validate_deploy`):

```json
{"schema": "pk-deploy/1", "family": "sgrb2", "portal": "main", "target": "/abs/target",
 "builds": [{"build_id": "20261010T120000Z-0123abcd", "deployed": "2026-10-10T12:30:00Z",
             "portalkit": "0.1.0+g1234abcd", "adapter_sha": "...", "legacy_modules": [],
             "replace": "stub"}],
 "files": {"map.html": {"sha256": "...", "size": 1234, "build_id": "20261010T120000Z-0123abcd"}},
 "replaced": [{"legacy": "../sgrb2_map.html", "backup": ".portalkit_backup/<build>/sgrb2_map.html",
               "md5": "...", "mode": "stub", "size": 5678, "build_id": "..."}]}
```

* Deploy writes the manifest twice. The first write registers the new files and a build
  entry with `"note": "in progress"` before any data is written; the second removes the note.
  An interrupted deploy therefore leaves a manifest that owns every file it may have written.
* When a second portal deploys into a target whose primary manifest belongs to another
  portal, its manifest is `.portalkit-deploy.<portal>.json`. Files owned by one manifest are
  conflicts for the other.
* `replaced[].legacy` is relative to the target; `replaced[].backup` is relative to
  `legacy_dir`.

### .htaccess

`--htaccess` writes the section 11.5 cache-header block to `<target>/.htaccess`, behind a
first-line marker `# portalkit: managed file (pk-htaccess/1) ...`. An existing `.htaccess`
without the marker is an ownership conflict (exit 4). The file is not listed in the manifest
because Apache refuses to serve `.ht*` files and `verify --http` could not fetch it.

## 5. Verify

```
portalkit verify FAMILY [--portal ID] [--http] [--auth-env PK_BASIC_AUTH]
```

Local checks: manifest valid; every owned file present, regular and matching its sha256; no
`.pk-tmp` leftovers; replaced legacy files present; the target exposure still satisfies the
portal visibility.

HTTP checks (`--http`, urllib only): each owned file is downloaded to a temporary file and
then hashed.

* secure portals: every URL must answer 401 without credentials. With `PK_BASIC_AUTH=user:pass`
  every URL must also answer 200 and match the manifest sha256.
* public portals: every URL must answer 200 and match.
* `Content-Encoding` on a `.bin` is a failure (the viewer reads `.bin` with Range requests).
  Requests send `Accept-Encoding: gzip` so this misconfiguration shows up; a gzip-encoded
  JSON or HTML body matches when its decoded bytes have the manifest sha256.
* `Cache-Control` is compared with the `.htaccess` rules and the report says whether the
  headers took effect. Missing headers are a note: content hashing gives correct caching
  either way.

## 6. Replace legacy pages

```
portalkit deploy FAMILY BUILD_ID --replace          # stub (default)
portalkit deploy FAMILY BUILD_ID --replace=inplace
```

For each `[[portal.page]]` with `legacy = [...]` whose unified page was built:

1. The original goes to `<legacy_dir>/.portalkit_backup/<build_id>/<file>` (mode and mtime
   kept) and `backup.json` (schema `pk-backup/1`) records its md5, size, mtime, the page id,
   the unified file and the sha256 of what deploy wrote over it.
2. `stub` writes a 300-byte page that calls
   `location.replace('<unified url>'+location.search+(location.hash||'#<preset>'))`, with a
   `<noscript>` link. `inplace` writes the unified page with every relative URL prefixed so
   it works from the legacy directory.
3. The manifest gains one `replaced` row per file.

Re-running the same build keeps the first backup, so a second run never backs up a stub as
the "original". Legacy paths that escape `legacy_dir`, lie inside the target, or are symlinks
are refused; missing legacy files produce a warning and no stub.

## 7. Roll back

```
portalkit rollback FAMILY BUILD_ID [--portal ID]
```

1. Every backup copy is checked against its md5 first; a mismatch or a missing copy stops the
   rollback before any file changes (exit 1).
2. A legacy file whose content differs from both the stub deploy wrote and the original
   was edited after the replace; rollback refuses (exit 4) unless `force=True`.
3. Each original is restored atomically with its mode and mtime, and its md5 is checked.
4. `backup.json` gains `rolled_back`; the manifest drops the build's `replaced` rows and the
   build entry gets `"note": "replace rolled back <time>"`. Rollback undoes the replace only:
   the unified files of that build stay in the target and are still served, so the build
   still counts as deployed for `status`, `verify` and `deploy --gc`.
5. The manifest updated is the one in the `target` recorded in `backup.json` (the directory
   the deploy wrote), unless `rollback_portal(..., target=...)` names another one.

Roll back newer builds first when several builds replaced the same legacy file: the backup of
the later build holds the earlier stub.

portalkit never moves or deletes legacy data directories.

## 8. Status

`portalkit status [FAMILY] [--json]` lists, per portal: legacy files (mtime, sha256, stub or
not), the newest staged build and its tree sha, the newest deployed build and the tree sha of
its files as found on disk. Flags:

| Flag | Meaning |
|---|---|
| `NOT-STAGED` | no staged build for this portal |
| `NOT-DEPLOYED` | no ownership manifest, or no completed build in it |
| `MODIFIED` | an owned file is missing or differs from the manifest |
| `DRIFT` | staged tree sha differs from the served tree sha |
| `LEGACY-CHANGED` | a legacy generator module differs from the sha recorded at deploy |
| `excluded` | the site registry marks the family out of scope |

A tree sha is the sha256 of `"<rel>\0<sha256>\n"` lines sorted by path. `deep=False` trusts
the manifest instead of re-hashing the deployed files.

## Python API

```python
from portalkit.deploy import (scan, classify, plan_deploy, verify, rollback, rollback_portal,
                              portal_status, DevServer)
from portalkit.deploy.deploy import deploy          # also exported as portalkit.deploy.run_deploy
from portalkit.deploy.status import status          # also exported as portalkit.deploy.status_rows

res = deploy("sgrb2", build_id, site, dry_run=True)       # site = parsed sites/sgrb2.toml
print(res.plan.summary())
rep = verify("sgrb2", site, http=True)
print(rep.format()); raise SystemExit(rep.exit_code)
```

The package does not re-export the functions `deploy` and `status` under those names, so
`import portalkit.deploy.deploy as m` and `import portalkit.deploy.status as m` bind the
submodules (section 2 layout).

### Shared legacy directories (section 4.1)

When portals of two families share a `legacy_dir` (WD2 and NIRSpec 2640), each family
deploys to `<legacy_dir>/unified/<family>/`. `deploy`, `plan_deploy`, `verify`,
`portal_status` and `rollback_portal` take `shared_legacy_dir=True` for that case; the CLI
computes it from all registries:

```python
from portalkit.deploy import is_shared_legacy_dir, load_sites, select_portal
entry = select_portal(site, portal_id)
shared = is_shared_legacy_dir(entry, load_sites())        # every sites/*.toml
```

`status()` detects shared legacy dirs itself over the registries it is given, or over
`all_sites=` when `portalkit status FAMILY` passes a single family. `rollback_portal` uses
the target recorded in `backup.json` first, so it finds the right manifest either way.

### Immutable publishes (section 11.3)

`portalkit publish-assets` and `publish-res` call `publish_assets(src, dest_root, version)`
(writes `<dest_root>/v<version>/`) and `publish_immutable(src, dest)`. Files with identical
bytes are skipped and new files are written atomically. When any existing file would change,
nothing is written and `OwnershipConflict` (exit 4) lists the files. Symlinks in the source or
in the destination path are refused (exit 3). With `emap=` the destination must be publicly
served.

Every refusal raises a `DeployError` subclass whose `exit_code` matches the table above
(`UsageError` 2, `ExposureRefusal`/`SymlinkRefusal` 3, `OwnershipConflict` 4,
`BlockedInput` 5, `VerifyFailure` 1); refusals raised by `deploy` carry the plan as `.plan`.

## Differences from ARCHITECTURE.md

* `backup.json` carries a schema id, `pk-backup/1`, and records `written_sha256` so rollback
  can detect edits made after the replace.
* Secondary manifests for extra portals in one target are named
  `.portalkit-deploy.<portal>.json`.
* `build_report.json` is not deployed.
* `.htaccess` ownership is recorded by its marker line instead of a manifest entry.
* The exposure scan maps directory symlinks only; a file symlink into a served root does not
  expose its directory.
* Build entries use `note` values `in progress` (an incomplete deploy) and
  `replace rolled back <time>` (the replace was undone; the build is still deployed).
* `verify --http` never sends `PK_BASIC_AUTH` to a public portal's URLs: they must answer 200
  without credentials.
* `UsageError` (exit 2) is an extra exception class for bad arguments.
* The devserver `gzip` mode compresses every response, `.bin` included, when the client sends
  `Accept-Encoding: gzip`, so tests can reproduce a host that applies mod_deflate broadly.
