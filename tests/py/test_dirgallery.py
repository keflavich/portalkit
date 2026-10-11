"""portalkit.legacy.dirgallery: image sets from directories of named files."""

import struct
import zlib

import pytest

from portalkit.legacy import dirgallery as dg
from portalkit.spec import ImageSetSpec, Preset


def png_bytes(w: int, h: int) -> bytes:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\x80" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def gif_bytes(w: int, h: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", w, h) + b"\x00" * 20


def jpeg_bytes(w: int, h: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    sof = b"\xff\xc0" + struct.pack(">HBHH", 11, 8, h, w) + b"\x01\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


@pytest.fixture
def gal(tmp_path):
    d = tmp_path / "gal"
    d.mkdir()
    for i in (1, 2, 10):
        (d / f"src{i}_spec.png").write_bytes(png_bytes(30 + i, 20))
        (d / f"src{i}_cutout.gif").write_bytes(gif_bytes(16, 12))
    (d / "src2_extra.jpg").write_bytes(jpeg_bytes(64, 48))
    (d / "notes.txt").write_text("not an image")
    (d / "other.png").write_bytes(png_bytes(4, 4))
    sub = d / "deep"
    sub.mkdir()
    (sub / "src3_spec.png").write_bytes(png_bytes(5, 5))
    return d


def test_image_size(tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(png_bytes(7, 3))
    assert dg.image_size(p) == (7, 3)
    p.write_bytes(gif_bytes(9, 4))
    assert dg.image_size(p) == (9, 4)
    p.write_bytes(jpeg_bytes(640, 480))
    assert dg.image_size(p) == (640, 480)
    p.write_bytes(b"nothing")
    assert dg.image_size(p) is None
    assert dg.image_size(tmp_path / "missing.png") is None


def test_natural_key():
    assert sorted(["s10", "s2", "S1"], key=dg.natural_key) == ["S1", "s2", "s10"]


def test_scan_roles_and_facets(gal):
    spec = dg.scan(gal, r"(?P<id>src\d+)_(?P<role>spec|cutout)\.(png|gif)", key="g",
                   url_prefix="https://example.org/gal/", roles={"spec": "Spectrum"},
                   caption="Source {id}", catalog="src", join="id")
    assert isinstance(spec, ImageSetSpec)
    assert spec.key == "g" and spec.catalog == "src" and spec.join == "id"
    assert [it.id for it in spec.items] == ["src1", "src2", "src10"]
    it = spec.items[0]
    assert it.roles == {"spec": "https://example.org/gal/src1_spec.png",
                        "cutout": "https://example.org/gal/src1_cutout.gif"}
    assert it.caption == "Source src1"
    assert (it.w, it.h) == (16, 12)                       # first file in natural order
    roles = dict(spec.roles)
    assert roles["spec"] == "Spectrum" and roles["cutout"] == "Cutout"


def test_scan_default_ids_and_tags(gal):
    spec = dg.scan(gal, r"(?P<target>src\d+)_spec\.png")
    assert [it.id for it in spec.items] == ["src1_spec", "src2_spec", "src10_spec"]
    assert spec.items[1].tags == {"target": "src2"}
    assert spec.items[1].caption == "src2_spec"
    assert dict(spec.roles) == {"main": "Image"}
    assert [f[0] for f in spec.facets] == ["target"]


def test_scan_recursive(gal):
    spec = dg.scan(gal, r"(deep/)?(?P<id>src\d+)_spec\.png", recursive=True, sizes=False)
    assert [it.id for it in spec.items] == ["src1", "src2", "src3", "src10"]
    assert spec.items[2].roles["main"] == "deep/src3_spec.png"
    assert spec.items[0].w is None


def test_scan_exts_and_presets(gal):
    p = [Preset(key="all", label="All", kind="filter", data={})]
    spec = dg.scan(gal, r"(?P<id>src\d+)_extra\.jpg", presets=p)
    assert [it.id for it in spec.items] == ["src2"] and (spec.items[0].w, spec.items[0].h) == (64, 48)
    assert spec.presets == p
    with pytest.raises(dg.DirGalleryError):
        dg.scan(gal, r"(?P<id>src\d+)_extra\.jpg", exts=(".png",))


def test_scan_errors(gal, tmp_path):
    with pytest.raises(dg.DirGalleryError, match="not a directory"):
        dg.scan(tmp_path / "nope", r".*")
    with pytest.raises(dg.DirGalleryError, match="no file"):
        dg.scan(gal, r"zzz\.png")
    with pytest.raises(dg.DirGalleryError, match="facets"):
        dg.scan(gal, r"(?P<id>src\d+)_spec\.png", facets=["missing"])
    with pytest.raises(dg.DirGalleryError, match="two files"):
        dg.scan(gal, r"(?P<id>src\d+)_.*\.(png|gif)")
    with pytest.raises(dg.DirGalleryError, match="caption"):
        dg.scan(gal, r"(?P<id>src\d+)_spec\.png", caption="{nope}")
    assert issubclass(dg.DirGalleryError, ValueError)
