"""portalkit.render.shell and web/shell.html (ARCHITECTURE.md sections 4.3, 6.1, 5.11)."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser

import pytest

from portalkit import caps, config
from portalkit.constants import PAGE_HTML_MAX
from portalkit.render import shell
from portalkit.spec import PageSpec, ReadmeSpec, catalog_from_rows

PORTAL = {"id": "test", "title": "Test & <portal>", "visibility": "public"}


def _doc(title="Map", budget=None, links=None, readme="# About", **kw):
    rows = [{"name": f"s{i}", "ra": 1.0 + i, "dec": 2.0, "f182m": 15.0 + i, "f410m": 13.0} for i in range(5)]
    cat = catalog_from_rows(rows, id="name", ra="ra", dec="dec", roles={"f182m": "mag", "f410m": "mag"})
    ds = {"src": cat, "readme": ReadmeSpec(markdown=readme)}
    page = PageSpec(id="map", archetype="explorer", title=title, datasets=["src", "readme"],
                    links=links or {}, **kw)
    res = caps.resolve(page, ds, {})
    return config.assemble(page, ds, {}, res, portal=PORTAL, budget=budget)


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts: list[tuple[dict, str]] = []
        self._cur = None
        self.body_attrs: dict = {}
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self._cur = (dict(attrs), [])
        if tag == "body":
            self.body_attrs = dict(attrs)
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "script" and self._cur is not None:
            self.scripts.append((self._cur[0], "".join(self._cur[1])))
            self._cur = None
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._cur is not None:
            self._cur[1].append(data)
        if self._in_title:
            self.title += data


def _parse(html):
    p = _Scripts()
    p.feed(html)
    p.close()
    return p


def _embedded(html):
    p = _parse(html)
    blobs = [body for attrs, body in p.scripts if attrs.get("id") == "pk-page"]
    assert len(blobs) == 1
    return json.loads(blobs[0])


def test_template_size_and_markers():
    tpl = shell.load_template()
    assert len(tpl.encode("utf-8")) < shell.TEMPLATE_MAX
    names = set(re.findall(r"\{\{([a-z_]+)\}\}", tpl))
    assert names == set(shell._FIELDS)
    assert '<meta name="viewport"' in tpl
    assert "<noscript>" in tpl
    assert 'type="module"' in tpl and "nomodule" in tpl
    assert "pk:theme" in tpl and "data-theme" in tpl
    for css in ("tokens.css", "base.css", "layout.css", "print.css"):
        assert "{{assets}}css/" + css in tpl
    assert tpl.count("onerror=") == 3
    # no globals (section 6): failures are reported through a DOM event
    assert "window.__" not in tpl and "pk-fail" in tpl


def test_render_round_trip_and_no_markers():
    doc = _doc()
    html = shell.render(doc)
    assert re.search(r"\{\{[a-z_]+\}\}", html) is None
    assert _embedded(html) == doc
    p = _parse(html)
    assert p.body_attrs["data-caps"].split() == doc["caps"]["enabled"]
    assert p.body_attrs["data-page"] == "map" and p.body_attrs["data-archetype"] == "explorer"
    assert p.title == "Map · Test & <portal>"
    assert "Test &amp; &lt;portal&gt;" in html
    mod = [b for a, b in p.scripts if a.get("type") == "module"][0]
    assert 'import("https://data.rc.ufl.edu/pub/adamginsburg/portalkit/v1.0.0/js/core/boot.js")' in mod
    assert html.startswith("<!doctype html>")


def test_script_breakout_escaped():
    hostile = "</script><script>alert(1)</script><!-- x"
    doc = _doc(title=hostile, links={"x": "p.html#id={id}"})
    doc["page"]["subtitle"] = hostile
    html = shell.render(doc)
    p = _parse(html)
    assert not any("alert(1)" in body for attrs, body in p.scripts if attrs.get("id") != "pk-page")
    assert len([1 for a, _ in p.scripts if a.get("id") == "pk-page"]) == 1
    blob = [b for a, b in p.scripts if a.get("id") == "pk-page"][0]
    assert "</" not in blob and "<!--" not in blob
    assert _embedded(html)["page"]["title"] == hostile
    assert p.title.startswith(hostile)
    # the hostile text appears only escaped (title/meta) or inside the JSON blob
    assert len(p.scripts) == len(_parse(shell.render(_doc())).scripts)
    outside = html.replace(blob, "")
    assert "<script>alert(1)" not in outside


def test_escape_json():
    s = shell.escape_json({"a": "</script>", "b": "<!--", "c": "ünï ☃  "})
    assert s == '{"a":"<\\/script>","b":"\\u003c!--","c":"ünï ☃  "}'
    assert json.loads(s) == {"a": "</script>", "b": "<!--", "c": "ünï ☃  "}
    with pytest.raises(ValueError):
        shell.escape_json({"x": float("nan")})


def test_budget_html_bytes_fixed_point():
    doc = _doc(budget={"first_paint_bytes": 99})
    html = shell.render(doc)
    emb = _embedded(html)
    assert emb["budget"]["html_bytes"] == len(html.encode("utf-8"))
    assert emb["budget"]["first_paint_bytes"] == 99
    assert doc["budget"]["html_bytes"] == 0   # input not mutated


def test_html_under_cap_for_typical_page():
    html = shell.render(_doc())
    assert len(html.encode("utf-8")) < 20000 < PAGE_HTML_MAX


def test_html_cap_enforced():
    tpl = shell.load_template() + "<!--" + "x" * PAGE_HTML_MAX + "-->"
    with pytest.raises(shell.ShellError, match="PAGE_HTML_MAX"):
        shell.render(_doc(), template=tpl)


def test_json_cap_enforced_without_validation():
    doc = _doc()
    doc["links"] = {f"l{i}": "p.html#id={id}" + "x" * 1000 for i in range(70)}
    with pytest.raises(shell.ShellError, match="PAGE_JSON_MAX"):
        shell.render(doc, validate=False)
    with pytest.raises(shell.ShellError, match="invalid page.json"):
        shell.render(doc)


def test_invalid_page_rejected():
    doc = _doc()
    del doc["caps"]
    with pytest.raises(shell.ShellError):
        shell.render(doc)
    doc2 = _doc()
    doc2["assets"] = "no-slash"
    with pytest.raises(shell.ShellError):
        shell.render(doc2, validate=False)


def test_unknown_placeholder():
    with pytest.raises(shell.ShellError, match="unknown placeholder"):
        shell.render(_doc(), template="<p>{{nope}}</p>")


def test_values_not_reexpanded():
    doc = _doc(title="{{assets}} {{page_json}}")
    html = shell.render(doc)
    p = _parse(html)
    assert p.title.startswith("{{assets}} {{page_json}}")


def test_unicode_page():
    doc = _doc(title="Ångström ☃ 銀河", readme="# Ünïcode\n\nε")
    html = shell.render(doc)
    assert _embedded(html)["page"]["title"] == "Ångström ☃ 銀河"
    assert "Ångström ☃ 銀河" in _parse(html).title


def test_write_atomic(tmp_path):
    doc = _doc(budget={"first_paint_bytes": 0})
    dest = tmp_path / "sub" / "map.html"
    n = shell.write(dest, doc)
    data = dest.read_bytes()
    assert n == len(data)
    assert _embedded(data.decode("utf-8"))["budget"]["html_bytes"] == n
    assert [x.name for x in dest.parent.iterdir()] == ["map.html"]
    n2 = shell.write(dest, _doc(title="Other"))
    assert n2 == len(dest.read_bytes())
    assert [x.name for x in dest.parent.iterdir()] == ["map.html"]


def test_write_failure_leaves_no_temp(tmp_path):
    doc = _doc()
    del doc["caps"]
    with pytest.raises(shell.ShellError):
        shell.write(tmp_path / "x.html", doc)
    assert list(tmp_path.iterdir()) == []
