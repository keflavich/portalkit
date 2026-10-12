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
  `/blue/adamginsburg/adamginsburg/portalkit_stage`. `stage.check_stage_root` refuses (exit 3) a stage
  root that the web server can reach.
* `build_report.json` sits next to `portal.json` in the staged portal (or in the build
  directory). It is the only staged file with full local input paths: served manifests and
  pages carry `<label>:<relative path>` provenance (`legacy_dir:`, `legacy:`, `input:`,
  `file:<basename>`; ARCHITECTURE section 9.3). Deploy reads it for provenance and never copies
  a file named `build_report.json` into the target, at any depth (`deploy.not_deployed`).
  `portalkit check` warns (G1) or fails (G2) when another staged `.html`/`.json` file holds an
  absolute `/orange/`, `/blue/` or `/home/` path. Deploy
  looks for these keys at the top level or under `provenance`:
  `adapter_sha`, `legacy_modules` (`[{name, path, sha256}]`), `portalkit`,
  `allow_tmp_inputs`, `inputs` (`[{path, sha256, ...}]` or paths).
* A build made with `--allow-tmp-inputs`, or one whose inputs lie under `/blue/*/tmp/`,
  `/tmp/` or `*/scratchpad/`, can be served for review and is refused by deploy (exit 5).

## 2. Review with the development server

```
portalkit serve /blue/adamginsburg/adamginsburg/portalkit_stage/FAMILY/BUILD_ID/PORTAL --port 8765
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
encoding, auth). `.ht*` paths and `..` components answer 403, and so does every path
`htaccess.denied` matches (ownership manifests, `.portalkit_backup/`, `*.pk-tmp`; section 4
".htaccess"), whether or not the served tree holds a `.htaccess`. Directory listings leave
those names out. Authentication is checked first, so a secure server answers 401 before 403.

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

Most of the cost of a scan is listing a few very large directories under
`web/secure/salt/` and `web/secure/ACES/`. Three measures keep it down without changing any
classification:

* **One listing per real directory.** Several symlink routes (and both roots) reach the same
  directories; each real directory is listed once per scan and its children are re-labelled
  for every route that reaches it. Symlinks are still resolved (`stat`, `readlink`,
  `realpath`) once per link, and every warning keeps the logical path of the route it was
  found on, so entries, `via` chains and warnings are identical to a walk that lists every
  route separately.
* **Result cache.** The result is cached for `cache_s` seconds (default an hour) in memory and
  in `$PK_CACHE_DIR/exposure-<key>.json` (default `~/.cache/portalkit/`). The key covers the
  roots, depth and prune globs. A scan with `$PK_CACHE_DIR` set elsewhere also reads, never
  writes, a matching file in `~/.cache/portalkit/`.
* **Directory stamps.** The cache file (format version 3; older files are ignored) records,
  per listed directory, `(st_dev, st_ino, st_mtime_ns, st_ctime_ns)` and the names of its
  subdirectories and symlinks. After `cache_s` expires the next scan `stat`s each directory
  and reuses the recorded names when the whole stamp matches, so only changed directories
  are listed again. The ctime covers changes that leave the mtime alone: `chmod`/`chown` (a
  directory made unlistable is listed again and reported as `cannot list`, as in a fresh
  walk) and an entry change followed by a restored mtime (`touch -r`, `rsync -t`); users
  cannot set ctime. Every symlink is re-resolved on every scan, so a link whose target
  appeared, moved or disappeared is reclassified even though its own directory is
  unchanged. A directory whose mtime or ctime lies within 5 s of the time it was listed is
  never reused (timestamp granularity), and a malformed stamp is ignored (the directory is
  listed). One case changes no stamp: a change of the scanning user's group membership,
  which can make a directory listable or unlistable; run with `cache_s=0` after one.

`scan(cache_s=0)` disables all three caches (no read, no write) and lists everything.

* **`PK_EXPOSURE_CACHE_S`.** `scan()` without `cache_s` uses `exposure.default_cache_s()`:
  the value of `$PK_EXPOSURE_CACHE_S` in seconds when it parses as a finite number ≥ 0
  (fractions are truncated; `0` disables the caches as above), else 3600. Empty, non-numeric,
  NaN, infinite and negative values are ignored. An explicit `cache_s` argument always wins.
  It is a test and development convenience: `deploy` and the CLI keep the hour unless the user
  sets the variable. `tests/conftest.py` sets it to `86400` while tests marked `adapters` run
  (and removes it for other tests), unless the user set it, so the adapter tests reuse the
  user's last scan from `~/.cache/portalkit/` read-only instead of walking or revalidating
  the roots. A scan older than a day is revalidated as usual.
`ExposureMap.stats` reports the counts of the last walk (`scandir`, `stat`, `dirents`,
`readlink`, `realpath`, `reused`, `shared`, `dirs`, `wall_s`); a map read from a cache
file has empty stats.

Measured on the real roots (2026-10-11, depth 4, prune `*_hips`; call counts from wrapped
`os` functions, so `lstat` and `readlink` include the calls made inside `realpath`):

| Run | wall | `scandir` | dirents read | `stat` | `lstat` | `readlink` | `realpath` |
|---|---|---|---|---|---|---|---|
| before, cold filesystem cache | 589 s | 6439 | 1,032,422 | 32,567 | 52,005 | 9,185 | 3,073 |
| before, warm filesystem cache | 10.1 s | 6439 | 1,032,422 | 32,567 | 52,005 | 9,185 | 3,073 |
| after, no result cache, warm filesystem | 8.8 s | 5957 | 849,349 | 38,230 | 49,898 | 8,517 | 2,963 |
| after, expired result cache (stamps) | 6.2 s | 0 | 0 | 38,230 | 49,898 | 8,517 | 2,963 |

All four runs produced the same 3073 entries and 2190 warnings, in the same order. The
"after" rows used cache format 2 (stamps without ctime); format 3 reads the ctime from the
same `stat` call, so the call counts are unchanged.

The wall time depends mostly on whether the filesystem metadata is cached: the first row ran
with a cold metadata cache, the others right after it. The cold-cache cost of the new walk
was not measured, so the table does not show a reduction of the cold-metadata cost (the
608 s fixture setup of a cold robitaille adapter-test run). What it shows: the new walk lists
about 18% fewer entries, and a stamp revalidation lists none, but a revalidation still makes
about 38,000 `stat` and 50,000 `lstat` calls (the per-directory stamp checks and the
per-symlink `realpath`), and each of those can be a metadata-server round trip on cold
Lustre. A cold revalidation may therefore still take minutes. The case that avoids the walk
entirely is a result younger than `cache_s` in memory, in `$PK_CACHE_DIR`, or (read-only) in
`~/.cache/portalkit/`; the adapter-test speedup on a cold node comes mainly from that
read-only fallback when the user has scanned the same roots within the hour. Reusing the
recorded `realpath` of a symlink whose own `lstat` stamp and target ancestors are unchanged
would remove most of the remaining calls; it is not implemented. One data point: after the
switch to format 3, when no cache file of the current format existed, the robitaille adapter
tests took 625 s (host load average about 110), compared with 30 s for the same tests with a
usable cache file. That run did a full fresh walk; its walk time was not profiled separately.

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
`schema.validate_deploy`). It keeps the absolute `target` and the full `legacy_modules` paths
from `build_report.json`, because `status` compares those files for LEGACY-CHANGED. It sits in
the served target, so it is the one served JSON file the no-local-path rule (ARCHITECTURE
§9.3) does not cover; `portalkit check` scans the staged tree only.

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
because Apache refuses to serve `.ht*` files and `verify --http` could not fetch it. An
`.htaccess` written by an earlier portalkit version (same marker) is rewritten in place.

The file denies deploy's own bookkeeping files (after the cache-header block when there is
one):

```
# deploy bookkeeping (ownership manifests, backups, temporaries) is never served
<IfModule mod_alias.c>
  RedirectMatch 403 "/\.portalkit[^/]*(/|$)|\.pk-tmp$"
</IfModule>
```

* It covers the ownership manifests `.portalkit-deploy.json` and
  `.portalkit-deploy.<portal>.json`, which keep the absolute `target` and the full
  `legacy_modules` paths (the one served JSON exception to the no-local-path rule), anything
  under a `.portalkit_backup/` directory, and in-flight `*.pk-tmp` files. Requests for them
  answer 403. Every other owned file (no path segment starting with `.portalkit`) is
  unaffected; `htaccess.denied(rel)` applies the same pattern in Python.
* `RedirectMatch` belongs to the `FileInfo` override class, the class the cache-header
  directives (`Header`, `AddType`) already need, so the deny rule adds nothing to what
  `--htaccess` requires. A `Require all denied` rule would need `AuthConfig` instead.
* A plain deploy (no `--htaccess`) now writes a `.htaccess` too, so it needs `AllowOverride`
  to include `FileInfo`, or to be `None` (the whole file is then ignored and the manifest is
  served). Apache answers 500 for every request in a directory whose `.htaccess` uses an
  override class it does not allow, so a host that allows only `AuthConfig`, `Options` and
  `Indexes` would break every file of the target; `<IfModule mod_alias.c>` does not prevent
  this. The `.htaccess` files under `/orange/adamginsburg/web` use only those three classes,
  and Q4 (ARCHITECTURE §16) asked whether the host allows `FileInfo`. A probe on
  2026-10-12 (a throwaway directory under `web/public` holding this deny rule plus a
  `Header set`) got 200 for a normal file, 403 for a `.portalkit_x/` file and the extra
  header in the response, so the public tree allows `FileInfo`. The secure tree was not
  probed; `verify --http` reports a 5xx there if it differs. Set
  `PK_DENY_HTACCESS=0` (Python: `deploy(..., deny_htaccess=False)`) to deploy without the
  deny-only file; the plan then warns, for a target with no `.htaccess`, that the manifest is
  served. A deny-only file already written stays until it is removed by hand. `verify --http`
  reports 5xx answers from a directory holding a portalkit `.htaccess` as a probable
  `AllowOverride` problem (section 5).
* Deploy writes the rule with or without `--htaccess` (WPE4), before the first manifest
  write, so the manifest is never on disk without it where the host honours the file:

  | `<target>/.htaccess` | `--htaccess` | without `--htaccess` |
  |---|---|---|
  | absent | full file (cache headers + rule) | minimal file: marker + rule (`DENY_HTACCESS_TEXT`) |
  | portalkit's (marker) | rewritten to the full file | brought up to date; cache headers an earlier `--htaccess` wrote are kept |
  | foreign (no marker) | ownership conflict, exit 4 | left unchanged; plan warning |
  | symlink | refusal, exit 3 | left unchanged; plan warning |

  The dry-run summary lists `.htaccess (deny rule for deploy bookkeeping)` when the minimal
  file would be written or updated. With a foreign or symlinked `.htaccess` the manifest stays
  readable at `<url>/.portalkit-deploy.json` unless that file denies it; the warning names the
  pattern to add. `status` and `verify` read the manifest from disk, never over HTTP, so
  denying it changes nothing for them.
* Backups live in `<legacy_dir>/.portalkit_backup/`, outside the default target
  `<legacy_dir>/unified`, and `backup.json` records the absolute `legacy_dir` and `target`.
  Whenever `backup.py` writes there (a `--replace` deploy) it first writes
  `<legacy_dir>/.portalkit_backup/.htaccess` (`htaccess.write_backup_htaccess`: the marker and
  the rule, no cache headers). A foreign or symlinked file at that path, a symlinked
  `.portalkit_backup`, or a file in its place refuses the plan (exit 4 or 3) before anything
  is written.
* `verify --http` checks that the rule took effect (section 5).

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
* The ownership manifest URL (`<url>/.portalkit-deploy.json`, or the portal's secondary
  manifest) gets a `HEAD` request (`check_manifest_denied`). When the target holds the
  portalkit deny rule, 403 or 404 is expected and any other answer is a warning (the host
  ignores the `.htaccess`, for example `AllowOverride None`). A 5xx is reported as undecided
  (`manifest_denied` stays `None`) with a warning that `AllowOverride` probably rejects the
  `.htaccess`; `SERVED` means a 2xx answer, and other statuses are undecided too. Without the rule, a 200 is a
  warning. When owned files answer 5xx and the target holds a portalkit `.htaccess`, the same
  `AllowOverride` warning is added once. The report prints `manifest URL denied` or `manifest URL SERVED` and `WARN` lines;
  warnings do not fail `verify`. A secure portal checked without `PK_BASIC_AUTH` answers 401
  before the per-directory rule applies, so the rule is reported as not checked.
* Foreign record sets (ARCHITECTURE section 4.3, WPE6): every records ref with a `base` in a
  deployed page gets a `HEAD` of its index URL (resolved against the page URL). A 200 is
  required. From a secure portal without `PK_BASIC_AUTH`, a 401 is a note (the foreign index
  is secure too); credentials are sent only from secure portals. The other portal's own
  `verify` checks its shards.

## 6. Replace legacy pages

```
portalkit deploy FAMILY BUILD_ID --replace          # stub (default)
portalkit deploy FAMILY BUILD_ID --replace=inplace
```

For each `[[portal.page]]` with `legacy = [...]` whose unified page was built. A legacy item
is a file name or a table (ARCHITECTURE section 4.1):

```toml
[[portal.page]]
id = "gallery_6927"
preset = ""                                            # page default for every stub
legacy = [
    "pngs/gallery_6927.html",                          # stub appends the page preset
    {file = "pngs/gallery_6927_2221.html", preset = "role=c2221"},   # this stub only
    {file = "imaging/index.html", replace = false},   # never stubbed or backed up
]
```

`replace = false` items stay in place, and the plan lists them as a warning. Use it for a
legacy file that sits behind a symlink, for example robitaille `imaging/ ->
modelgrids/imaging_run/png`, which would otherwise refuse the whole replace (exit 3). Every
other item is processed as follows:

1. The original goes to `<legacy_dir>/.portalkit_backup/<build_id>/<file>` (mode and mtime
   kept) and `backup.json` (schema `pk-backup/1`) records its md5, size, mtime, the page id,
   the unified file and the sha256 of what deploy wrote over it.
2. `stub` writes a 300-byte page that calls
   `location.replace('<unified url>'+location.search+(location.hash||'#<preset>'))`, where
   `<preset>` is the item's own preset if it has one, else the page preset, with a
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

## Family runbooks

### spherex (replaces `deploy_portal.sh`)

The legacy SPHEREx portal was mirrored by `/orange/adamginsburg/spherex/deploy_portal.sh`:
`rsync -a --delete --exclude README.md portal/ web/public/spherex/portal/`, then `curl`
checks of six served URLs and one spectrum JSON. portalkit replaces it (sites/spherex.toml;
public portal, target `web/public/spherex/portal/unified/`):

```bash
python -m portalkit build spherex                            # prints BUILD_ID; stages the portal
python -m portalkit deploy spherex <BUILD_ID> --dry-run      # plan, exposure (public) and ownership checks
python -m portalkit deploy spherex <BUILD_ID>                # writes the target; the README is deployed too
python -m portalkit verify spherex --http                    # every owned file answers 200 with its sha256
```

Differences from the script:
- `deploy` writes only into `unified/` and records ownership; it never deletes files it did
  not write (the script's `--delete` removed anything else in the served tree).
- `verify --http` checks every owned file (pages, catalog chunks, spectra shards), where the
  script checked six URLs and one spectrum.
- The README is generated by the adapter and deployed with the pages (the script excluded
  it).
- Legacy pages stay in place until `deploy spherex <BUILD_ID> --replace` turns them into
  redirect stubs (section 6); `deploy_portal.sh` is not run again after the switch, since an
  rsync `--delete` of `portal/` would remove the `unified/` target and the stubs.

### jwst_archive: the legacy portal is reachable under /pub/

`mastDownload/JWST/pngs/portal` is a symlink to `web/secure/jwst/spectra/portal`, and
`mastDownload/JWST/pngs` is public through `web/public/jwst/spectra`. So the legacy archive
portal (`/orange/adamginsburg/jwst/spectra/portal`: `index.html`, `archive_manifold.html`,
`spectra/`) is served both under `/secure/` (login) and under
`https://data.rc.ufl.edu/pub/adamginsburg/jwst/spectra/portal/` without a login. This is
recorded as a user decision (WPE6): the legacy pages stay publicly reachable through
`pngs/portal`, and portalkit does not remove the symlink. Because of that symlink the unified archive
portal deploys to `jwst/spectra/portal_unified` (classified `secure` only); a target under
`portal/` would classify `both`, and a secure deploy there is refused (section 3, exit 3).

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
* `build_report.json` is not deployed (at any depth of the staged tree).
* `legacy` items may be `{file, preset?, replace?}` tables (WPE1; now also in ARCHITECTURE
  section 4.1).
* `.htaccess` ownership is recorded by its marker line instead of a manifest entry.
* The portalkit `.htaccess` carries a deny rule for `.portalkit*` paths and `*.pk-tmp` after
  the section 11.5 block (WPE3). Deploy writes a minimal managed `.htaccess` with only that
  rule when `--htaccess` is not passed, and it writes `.htaccess` before the manifest in both
  cases (WPE4). Without `--htaccess` a foreign `.htaccess` is a warning.
* `verify --http` warns when the manifest URL is served (WPE4); warnings do not fail it.
* `verify --http` HEADs the index of each foreign record set (WPE6).
* `scan()`'s default `cache_s` follows `$PK_EXPOSURE_CACHE_S` (WPE4, section 3).
* The exposure scan maps directory symlinks only; a file symlink into a served root does not
  expose its directory.
* The exposure cache file carries per-directory stamps (format version 3: dev, inode, mtime,
  ctime) that let an expired
  result be revalidated without listing unchanged directories, and a scan with a custom
  `$PK_CACHE_DIR` also reads `~/.cache/portalkit/` (WPE3, section 3).
* Build entries use `note` values `in progress` (an incomplete deploy) and
  `replace rolled back <time>` (the replace was undone; the build is still deployed).
* `verify --http` never sends `PK_BASIC_AUTH` to a public portal's URLs: they must answer 200
  without credentials.
* `UsageError` (exit 2) is an extra exception class for bad arguments.
* The devserver `gzip` mode compresses every response, `.bin` included, when the client sends
  `Accept-Encoding: gzip`, so tests can reproduce a host that applies mod_deflate broadly.
