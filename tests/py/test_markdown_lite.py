"""portalkit.render.markdown_lite: supported subset and sanitisation."""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest

from portalkit.render import markdown_lite as md


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict]] = []
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)


def _parse(html: str) -> _Tags:
    p = _Tags()
    p.feed(html)
    p.close()
    return p


ALLOWED_ATTRS = {"id", "start", "style", "class", "href", "title", "rel"}


def _assert_safe(html: str) -> None:
    p = _parse(html)
    for tag, attrs in p.tags:
        assert tag in md.ALLOWED_TAGS, (tag, html)
        assert set(attrs) <= ALLOWED_ATTRS, (tag, attrs)
        if "href" in attrs:
            assert md.safe_url(attrs["href"]) is not None, attrs["href"]
        if "style" in attrs:
            assert re.fullmatch(r"text-align:(left|right|center)", attrs["style"])
        if "class" in attrs:
            assert re.fullmatch(r"language-[A-Za-z0-9_+#.-]+", attrs["class"])
    for bad in ("<script", "<img", "<iframe", "<svg", "<style"):
        assert bad not in html.lower(), (bad, html)


def test_headings_with_slugs_and_dedup():
    out = md.render("# Hello World\n## Hello  world!\n### Hello World\n#### C# and F#")
    ids = [a["id"] for t, a in _parse(out).tags]
    assert ids == ["hello-world", "hello-world-1", "hello-world-2", "c-and-f"]
    assert "<h4" in out and "C# and F#" in out
    assert md.render("# Title ###") == '<h1 id="title">Title</h1>'


def test_unicode_heading_slug():
    out = md.render("# Ångström ε-sources ☃")
    assert 'id="angstrom-sources"' in out
    assert "Ångström ε-sources ☃" in out
    assert 'id="section"' in md.render("# ☃")


def test_paragraphs_and_escaping():
    out = md.render("a < b & c > d\nsecond line\n\nnew para")
    assert out == "<p>a &lt; b &amp; c &gt; d\nsecond line</p>\n<p>new para</p>"


def test_raw_html_is_text():
    out = md.render('<script>alert(1)</script>\n\n<img src=x onerror="alert(1)">')
    assert "<script" not in out and "<img" not in out
    assert "&lt;script&gt;" in out
    _assert_safe(out)


def test_lists_nested_and_ordered():
    out = md.render("- a\n- b\n  - b1\n  - b2\n- c\n\n3. x\n4. y")
    assert out.startswith("<ul><li>a</li><li>b<ul><li>b1</li><li>b2</li></ul></li><li>c</li></ul>")
    assert '<ol start="3"><li>x</li><li>y</li></ol>' in out
    _assert_safe(out)


def test_list_kind_switch_and_continuation():
    out = md.render("1. one\n   more\n- dash")
    assert out == "<ol><li>one\nmore</li></ol><ul><li>dash</li></ul>"


def test_table_alignment_and_pipes():
    out = md.render("| a | b | c |\n|:--|:-:|--:|\n| 1 | `x|y` | 3 |\n| \\| | | |")
    assert "<table><thead><tr>" in out
    assert '<th style="text-align:left">a</th>' in out
    assert '<td style="text-align:center"><code>x|y</code></td>' in out
    assert '<td style="text-align:right">3</td>' in out
    assert '<td style="text-align:left">|</td>' in out
    _assert_safe(out)


def test_table_short_rows_padded():
    out = md.render("a | b\n--- | ---\n1")
    assert out.count("<td>") == 2


def test_table_needs_matching_delimiter():
    out = md.render("a | b\n--- | --- | ---")
    assert "<table" not in out


def test_fenced_code_escapes_and_language():
    out = md.render("```python\n</script><b>\n  x = 1\n```\nafter")
    assert '<pre><code class="language-python">&lt;/script&gt;&lt;b&gt;\n  x = 1</code></pre>' in out
    assert "<p>after</p>" in out
    hostile = md.render('```"><script>\nx\n```')
    assert "<script" not in hostile
    _assert_safe(hostile)


def test_unclosed_fence_runs_to_end():
    out = md.render("~~~\ncode\n# not a heading")
    assert out == "<pre><code>code\n# not a heading</code></pre>"


def test_inline_code_and_backslash_escape():
    out = md.render_inline("use `a < b` and \\[not a link\\](x)")
    assert out == "use <code>a &lt; b</code> and [not a link](x)"
    assert md.render_inline("``a ` b``") == "<code>a ` b</code>"
    assert md.render_inline("unclosed `tick") == "unclosed `tick"


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "JaVaScRiPt:alert(1)", "java\tscript:alert(1)", " javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>", "vbscript:x", "//evil.example/x", "file:///etc/passwd",
    "\x01javascript:alert(1)",
])
def test_unsafe_link_schemes_dropped(url):
    out = md.render_inline(f"[click]({url})")
    assert "<a" not in out
    assert "click" in out
    _assert_safe(out)


@pytest.mark.parametrize("url,expect", [
    ("https://example.org/a?b=1&c=2", 'href="https://example.org/a?b=1&amp;c=2"'),
    ("page.html#id=3", 'href="page.html#id=3"'),
    ("#section", 'href="#section"'),
    ("mailto:a@b.org", 'href="mailto:a@b.org"'),
    ("../up.html", 'href="../up.html"'),
])
def test_safe_links(url, expect):
    out = md.render_inline(f"[t]({url})")
    assert expect in out
    _assert_safe(out)


def test_link_title_quotes_and_rel():
    out = md.render_inline('[x](https://a.org/ "say \\"hi\\" <b>")')
    assert 'title="say &quot;hi&quot; &lt;b&gt;"' in out
    assert 'rel="noopener"' in out
    assert "<b>" not in out


def test_link_text_inline_markup_and_no_nesting():
    out = md.render_inline("[`code` [inner](b.html)](a.html)")
    assert out.count("<a ") == 1
    assert "<code>code</code>" in out


def test_autolink():
    out = md.render_inline("see <https://data.rc.ufl.edu/pub/x> and <mailto:a@b.org> and <b>")
    assert '<a href="https://data.rc.ufl.edu/pub/x" rel="noopener">https://data.rc.ufl.edu/pub/x</a>' in out
    assert '<a href="mailto:a@b.org">a@b.org</a>' in out
    assert "&lt;b&gt;" in out


def test_attribute_breakout_in_href():
    out = md.render_inline('[x](https://a.org/"onmouseover="alert(1))')
    p = _parse(out)
    for tag, attrs in p.tags:
        assert "onmouseover" not in attrs
    _assert_safe(out)


def test_emphasis_stays_literal():
    assert md.render_inline("*a* **b** _c_") == "*a* **b** _c_"


def test_horizontal_rule_dropped():
    assert md.render("a\n\n---\n\nb") == "<p>a</p>\n<p>b</p>"


def test_empty_and_whitespace():
    assert md.render("") == ""
    assert md.render("\n\n   \n") == ""


def test_crlf_and_nul():
    out = md.render("# a\r\n\r\nb\x00c")
    assert out == '<h1 id="a">a</h1>\n<p>b�c</p>'


def test_type_error():
    with pytest.raises(TypeError):
        md.render(None)


def test_hostile_fuzz_is_safe():
    import random
    rng = random.Random(1234)
    alphabet = list("<>\"'`[]()#|-*_:\\/ \nabcjavascript=onerror!~1.") + ["javascript:", "<script>", "](", "```"]
    for _ in range(300):
        s = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 80)))
        out = md.render(s)
        _assert_safe(out)


def test_large_input():
    text = "\n\n".join(f"## Section {i}\n\n- item {i}\n- [link](p{i}.html)" for i in range(500))
    out = md.render(text)
    assert out.count("<h2") == 500
    assert 'id="section-499"' in out



def test_list_interrupts_paragraph():
    from portalkit.render import markdown_lite as M
    assert M.render("The archive contains:\n- spectra\n- images\n") == \
        "<p>The archive contains:</p>\n<ul><li>spectra</li><li>images</li></ul>"
    assert M.render("Steps:\n1. download\n2. reduce\n") == \
        "<p>Steps:</p>\n<ol><li>download</li><li>reduce</li></ol>"
    # an ordered item not numbered 1, or an empty item, does not interrupt a paragraph
    assert M.render("In\n2020. was a year") == "<p>In\n2020. was a year</p>"
    assert M.render("a\n-\nb") == "<p>a\n-\nb</p>"


def test_backslash_protocol_relative_dropped():
    from portalkit.render import markdown_lite as M
    assert "href" not in M.render("[a](/\\evil.com)")
    for u in ("/\\evil.com", "\\\\evil.com", "\\/evil.com", " /\\evil.com"):
        assert M.safe_url(u) is None, u
    assert 'href="/ok/path"' in M.render("[a](/ok/path)")
