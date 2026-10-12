"""Stellar templates -> ``templates/index.<h8>.json`` (``pk-templates/1``) + pks1 files.

ARCHITECTURE.md sections 5.10 and 9.4.

Source: ``jwst_template_spectra.json`` (Castelli-Kurucz templates on three resolution grids,
``prism`` R=100, ``medium`` R=1000, ``high`` R=2700, 0.6-5.3 um, flux normalised to a median
of 1 over 2.0-2.4 um; flux behaves as F_nu). Each grid also carries the G23 curves
``k31``/``k55`` (A(lambda)/A(V) for R_V 3.1/5.5) sampled on it.

Written files (all under ``templates/``; URLs in the index are relative to the index):

* ``grid_<grid>.<h8>.json``: the wavelength grid as a records-index grid descriptor
  (``{"kind":"array","n":N,"values":[...]}`` or ``{"kind":"uniform","w0","dw","n"}``);
* ``t_<template>_<grid>.<h8>.bin``: gzip pks1, ``wave = {"kind":"shared","grid":<grid>}``,
  one ``flux`` channel (``u16log``, unit ``""``), ``meta = {template, grid, teff, logg}``;
* ``law_<k31|k55>_<grid>.<h8>.bin``: gzip pks1 with one ``k`` channel (f32,
  A(lambda)/A(V)), ``meta = {law: "G23", rv}``;
* ``index.<h8>.json``.

``TemplateSpec.extend_to_um`` extends every grid, curve and template beyond 5.3 um with the
Brick2221 ``build_extended_templates`` scheme (grid continued at its median log step,
power-law extinction extrapolation from the last 50 samples, blackbody continuum scaled to
the last native sample). The blackbody is ``B_nu`` by default because the template flux is
F_nu. The Brick2221 code originally used ``B_lambda``, which under-predicts the continuum beyond
5.3 um (by a factor ~24 at 26 um for the last prism sample); Brick2221 now uses ``B_nu`` as
well, and the default output equals its ``build_extended_templates`` JSON content
(whitespace/float formatting is not compared). ``planck="lambda"`` reproduces the older
B_lambda curves.
"""

from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Mapping

import numpy as np

from . import PhysicsResult, _writer

__all__ = ["DEFAULT_SOURCE", "LAW_KEYS", "load_source", "source_label", "extend_templates",
           "build_extended_templates", "templates_document", "write", "planck_lambda",
           "planck_nu", "TEMPLATE_FILE_SOFT_MAX"]

DEFAULT_SOURCE = Path("/orange/adamginsburg/jwst/spectra/templates/jwst_template_spectra.json")
LAW_KEYS = {"k31": 3.1, "k55": 5.5}
TEMPLATE_FILE_SOFT_MAX = 40 * 1024

_PLANCK_H = 6.62607015e-34
_PLANCK_C = 2.99792458e8
_PLANCK_KB = 1.380649e-23


def planck_lambda(teff_K: float, wavelength_um: Any) -> np.ndarray:
    """B_lambda (W m^-3 sr^-1); verbatim from Brick2221 ``_planck_lambda``."""
    lam_m = np.asarray(wavelength_um) * 1e-6
    x = _PLANCK_H * _PLANCK_C / (lam_m * _PLANCK_KB * teff_K)
    return (2 * _PLANCK_H * _PLANCK_C**2 / lam_m**5) / np.expm1(x)


def planck_nu(teff_K: float, wavelength_um: Any) -> np.ndarray:
    """B_nu (W m^-2 Hz^-1 sr^-1) at ``wavelength_um``."""
    lam_m = np.asarray(wavelength_um) * 1e-6
    nu = _PLANCK_C / lam_m
    x = _PLANCK_H * nu / (_PLANCK_KB * teff_K)
    return (2 * _PLANCK_H * nu**3 / _PLANCK_C**2) / np.expm1(x)


def _extend_grid_wavelengths(w_native: Any, wmax_um: float) -> np.ndarray:
    """Continue the grid's own log-wavelength step out to wmax_um (Brick2221)."""
    w_native = np.asarray(w_native, dtype=float)
    dlogw = np.median(np.diff(np.log(w_native)))
    n_new = int(np.ceil((np.log(wmax_um) - np.log(w_native[-1])) / dlogw))
    return w_native[-1] * np.exp(dlogw * np.arange(1, n_new + 1))


def _extend_extinction_curve(w_native: Any, k_native: Any, w_new: np.ndarray,
                             nfit: int = 50) -> np.ndarray:
    """Power-law continuation of A(lambda)/A(V) from the last ``nfit`` samples (Brick2221).

    It carries the general mid-IR decline only (no silicate features)."""
    w_native = np.asarray(w_native, dtype=float)
    k_native = np.asarray(k_native, dtype=float)
    npts = min(nfit, len(k_native) - 2)
    slope, intercept = np.polyfit(np.log(w_native[-npts:]),
                                  np.log(np.clip(k_native[-npts:], 1e-6, None)), 1)
    k_new = np.exp(intercept + slope * np.log(w_new))
    return np.clip(k_new, 1e-4, None)


def _extend_template_flux(w_native: Any, f_native: Any, teff_K: float, w_new: np.ndarray,
                          planck: str = "nu") -> np.ndarray:
    """Blackbody continuation scaled to the last native sample (Brick2221 scheme)."""
    bb = planck_nu if planck == "nu" else planck_lambda
    w_native = np.asarray(w_native, dtype=float)
    f_native = np.asarray(f_native, dtype=float)
    scale = f_native[-1] / bb(teff_K, w_native[-1])
    return scale * bb(teff_K, w_new)


def extend_templates(doc: Mapping, wmax_um: float, planck: str = "nu") -> dict:
    """Copy of a template document with every grid, curve and template flux extended to
    ``wmax_um`` (see module docstring). ``planck`` is ``"nu"`` (default) or ``"lambda"``
    (the legacy Brick2221 behaviour)."""
    if planck not in ("nu", "lambda"):
        raise ValueError("planck must be 'nu' or 'lambda'")
    d = copy.deepcopy(dict(doc))
    wmax_um = float(wmax_um)
    new_w = {}
    for gkey, grid in d["grids"].items():
        w_native = grid["w"]
        if not wmax_um > float(w_native[-1]):
            raise ValueError(f"extend_to_um={wmax_um} must exceed the grid end {w_native[-1]}")
        w_new = _extend_grid_wavelengths(w_native, wmax_um)
        new_w[gkey] = w_new
        k31_ext = _extend_extinction_curve(w_native, grid["k31"], w_new)
        k55_ext = _extend_extinction_curve(w_native, grid["k55"], w_new)
        grid["w"] = list(w_native) + w_new.tolist()
        grid["k31"] = list(grid["k31"]) + k31_ext.tolist()
        grid["k55"] = list(grid["k55"]) + k55_ext.tolist()
        grid["n"] = len(grid["w"])
    for t in d["templates"]:
        teff = float(t["teff"])
        for gkey, grid in d["grids"].items():
            w_native = grid["w"][:len(grid["w"]) - len(new_w[gkey])]
            f_native = t["f"][gkey]
            f_ext = _extend_template_flux(w_native, f_native, teff, new_w[gkey], planck)
            t["f"][gkey] = list(f_native) + f_ext.tolist()
    meta = d.setdefault("meta", {})
    meta["wmax"] = wmax_um
    # "nu" matches the Brick2221 caveat text; "lambda" keeps the older B_lambda wording
    bbname = "B_nu, scaled" if planck == "nu" else "scaled"
    meta["caveat"] = (meta.get("caveat", "") +
                      f" Extended to {wmax_um}um by blackbody continuum ({bbname} to match "
                      "the native grid at 5.3um) + power-law extinction extrapolation; "
                      "this carries no PAH/silicate/ice features beyond 5.3um, only the "
                      "stellar continuum shape.").strip()
    return d


def build_extended_templates(src_json: str | Path, dest_json: str | Path, wmax_um: float = 26.0,
                             planck: str = "nu") -> str | Path:
    """Brick2221-compatible wrapper: read ``src_json``, extend, write ``dest_json``.

    The default reproduces the Brick2221 output: same arrays and the same caveat text.
    """
    with open(src_json) as fh:
        d = json.load(fh)
    out = extend_templates(d, wmax_um, planck=planck)
    with open(dest_json, "w") as fh:
        json.dump(out, fh)
    return dest_json


def source_label(path: str | os.PathLike,
                 labeler: Callable[[str], str] | None = None) -> str:
    """The served label of a template source path (``meta.source``).

    ``labeler`` (for example a :class:`portalkit.build.PathLabeler`) maps the path to
    ``<label>:<relpath>`` under a declared root. Without one, an absolute path becomes
    ``file:<basename>``, which is what ``PathLabeler`` writes for a path under no declared root
    (ARCHITECTURE.md section 9.3 "Served paths"); a ``file://`` URL is treated as its path, and a
    relative path is kept as given."""
    s = os.fspath(path)
    if labeler is not None:
        return str(labeler(s))
    if s.startswith("file://"):
        s = s[len("file://"):]
    if not s.startswith("/"):
        return s
    return f"file:{os.path.basename(os.path.normpath(s))}"


def load_source(spec: Any = None, *, planck: str = "nu",
                labeler: Callable[[str], str] | None = None) -> dict:
    """The template JSON for a TemplateSpec (source + optional extension).

    ``planck`` selects the blackbody used by ``extend_to_um`` (see :func:`extend_templates`).
    ``meta.source`` is the served label of the source path (:func:`source_label`; default
    ``file:<basename>``), never the absolute local path, so a direct :func:`write` (or
    ``physics.write_shared``) call outside the build stages no local path either.
    """
    src = getattr(spec, "source", None) if spec is not None else None
    if isinstance(spec, Mapping):
        src = spec.get("source")
    path = Path(src) if src is not None else DEFAULT_SOURCE
    with open(path) as fh:
        d = json.load(fh)
    for k in ("grids", "templates"):
        if k not in d:
            raise ValueError(f"{path}: template JSON has no {k!r}")
    ext = getattr(spec, "extend_to_um", None) if spec is not None else None
    if isinstance(spec, Mapping):
        ext = spec.get("extend_to_um")
    if ext is not None:
        d = extend_templates(d, float(ext), planck=planck)
    d.setdefault("meta", {})["source"] = source_label(path, labeler)
    return d


def _grid_file_obj(w: np.ndarray) -> dict:
    from ..bundle.records import grid_descriptor
    g = grid_descriptor(w)
    if g["kind"] == "array":
        g = {"kind": "array", "n": int(w.size), "values": g["values"]}
    return g


def _law_record(rec_id: str, grid: str, k: np.ndarray, rv: float) -> bytes:
    from ..bundle.records import MAGIC_PKS1, pack_record
    header = {"id": rec_id, "n": int(k.size), "wave": {"kind": "shared", "grid": grid},
              "unit_w": "um", "channels": [{"name": "k", "unit": "", "enc": "f32", "off": 0}],
              "meta": {"law": "G23", "rv": float(rv)}}
    return pack_record(MAGIC_PKS1, header, np.asarray(k, dtype="<f4").tobytes())


def _template_record(rec_id: str, grid: str, w: np.ndarray, f: np.ndarray, meta: dict) -> bytes:
    from ..bundle.records import encode_pks1
    spec = {"wave": w, "channels": {"flux": f}, "units": {"flux": ""}, "meta": meta}
    return encode_pks1(rec_id, spec, enc={"flux": "u16log"}, grids={grid: w})


def _check_array(name: str, a: Any, n: int, positive: bool = False) -> np.ndarray:
    arr = np.asarray(a, dtype=np.float64)
    if arr.shape != (n,):
        raise ValueError(f"{name}: expected {n} values, got shape {arr.shape}")
    if positive and np.any(~(np.isfinite(arr)) | (arr <= 0)):
        bad = int(np.sum(~np.isfinite(arr) | (arr <= 0)))
        raise ValueError(f"{name}: {bad} non-positive or non-finite values")
    return arr


def templates_document(src: Mapping, file_urls: Mapping[str, Any]) -> dict:
    """The ``pk-templates/1`` index for a source document and its written file URLs."""
    meta = {k: v for k, v in (src.get("meta") or {}).items()}
    meta.setdefault("flux", "F_nu, normalised (median over 2.0-2.4 um = 1)")
    grids = {}
    for g, gv in src["grids"].items():
        e = {"file": file_urls["grids"][g], "n": len(gv["w"])}
        if gv.get("R") is not None:
            e["R"] = gv["R"]
        grids[g] = e
    tpls = []
    for t in src["templates"]:
        entry = {"key": str(t["key"]), "teff": t.get("teff"), "logg": t.get("logg"),
                 "label": str(t.get("label", t["key"])),
                 "files": {g: file_urls["templates"][(t["key"], g)] for g in src["grids"]}}
        if t.get("kind") is not None:
            entry["kind"] = t["kind"]
        if t.get("note") is not None:
            entry["note"] = t["note"]
        tpls.append(entry)
    laws = {lk: {g: file_urls["laws"][(lk, g)] for g in src["grids"] if (lk, g) in file_urls["laws"]}
            for lk in LAW_KEYS}
    laws = {k: v for k, v in laws.items() if v}
    return {"schema": "pk-templates/1", "meta": meta, "grids": grids, "templates": tpls,
            "laws": laws}


def write(spec: Any, dest: Any, *, ledger: Any = None, planck: str = "nu",
          labeler: Callable[[str], str] | None = None) -> PhysicsResult:
    """Write the template set into ``dest/templates/``; ``path`` is ``templates/index.<h8>.json``.

    ``planck="lambda"`` gives legacy Brick2221 parity for ``extend_to_um`` (see module docstring).
    ``labeler`` labels ``meta.source`` (:func:`source_label`). Without one, a ``dest`` writer
    that carries a callable ``labeler`` attribute (``portalkit.build.LabellingWriter``, which
    the build passes through ``physics.write_shared`` and ``write_dataset``) supplies it, so
    the build stages the site's root label (``input:templates/...``) and not only
    ``file:<basename>``.
    """
    from ..bundle.codec import gz
    from ..schema import validate_pks1_header, validate_templates
    from ..bundle.records import split_record

    if labeler is None:
        dest_labeler = getattr(dest, "labeler", None)
        if callable(dest_labeler):
            labeler = dest_labeler
    src = load_source(spec, planck=planck, labeler=labeler)
    w = _writer(dest, ledger)
    tw = w.sub("templates")
    urls: dict[str, dict] = {"grids": {}, "templates": {}, "laws": {}}
    files: list[str] = []
    keys = [str(t["key"]) for t in src["templates"]]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate template keys")
    for g, gv in src["grids"].items():
        wv = np.asarray(gv["w"], dtype=np.float64)
        n = wv.size
        if n < 2 or not np.all(np.isfinite(wv)) or np.any(np.diff(wv) <= 0):
            raise ValueError(f"grid {g!r}: wavelengths must be finite and strictly increasing")
        rel = tw.put_json("", f"grid_{g}", _grid_file_obj(wv), kind="templates")
        urls["grids"][g] = rel
        files.append(f"templates/{rel}")
        for lk, rv in LAW_KEYS.items():
            if lk not in gv:
                continue
            k = _check_array(f"grid {g} {lk}", gv[lk], n)
            raw = _law_record(f"{lk}_{g}", g, k, rv)
            rel = tw.put("", f"law_{lk}_{g}", gz(raw), "bin", kind="templates")
            urls["laws"][(lk, g)] = rel
            files.append(f"templates/{rel}")
        for t in src["templates"]:
            key = str(t["key"])
            f = _check_array(f"template {key} grid {g}", t["f"][g], n, positive=True)
            meta = {"template": key, "grid": g}
            for mk in ("teff", "logg"):
                if t.get(mk) is not None:
                    meta[mk] = t[mk]
            raw = _template_record(f"{key}_{g}", g, wv, f, meta)
            _, hdr, _ = split_record(raw)
            errs = validate_pks1_header(hdr, {g: {}})
            if errs:
                raise ValueError(f"template {key} grid {g}: " + "; ".join(errs))
            data = gz(raw)
            if len(data) > TEMPLATE_FILE_SOFT_MAX:
                w.ledger.warn(f"templates: {key}/{g} is {len(data)} bytes "
                              f"(> {TEMPLATE_FILE_SOFT_MAX})")
            rel = tw.put("", f"t_{_slug(key)}_{g}", data, "bin", kind="templates")
            urls["templates"][(t["key"], g)] = rel
            files.append(f"templates/{rel}")
    doc = templates_document(src, urls)
    errs = validate_templates(doc)
    if errs:
        raise ValueError("templates index invalid: " + "; ".join(errs[:10]))
    rel = tw.put_json("", "index", doc, kind="templates")
    path = f"templates/{rel}"
    files.append(path)
    return PhysicsResult(path=path, doc=doc, files=files)


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_") or "t"

