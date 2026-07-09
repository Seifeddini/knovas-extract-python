"""Unit tests for structured-table extraction from HTML (spec 1.1.0+)."""

from __future__ import annotations

import pytest

from knovas_extract import extract

pytest.importorskip("selectolax")


def _html(fragment: str) -> bytes:
    return (
        b"<!doctype html><html><head><title>t</title></head><body>"
        + fragment.encode("utf-8")
        + b"</body></html>"
    )


@pytest.mark.unit
def test_html_no_tables_emits_none() -> None:
    r = extract(_html("<p>Just prose.</p>"), mime="text/html")
    assert r.content.tables is None


@pytest.mark.unit
def test_html_thead_tbody_table_extracted() -> None:
    frag = """
    <table>
      <thead><tr><th>Vendor</th><th>Invoice</th><th>Amount</th></tr></thead>
      <tbody>
        <tr><td>Acme</td><td>12345</td><td>500.00</td></tr>
        <tr><td>Bosch</td><td>12346</td><td>1200.00</td></tr>
      </tbody>
    </table>
    """
    r = extract(_html(frag), mime="text/html")
    assert r.content.tables is not None and len(r.content.tables) == 1
    t = r.content.tables[0]
    assert t.client_table_hint == "html_dom_t0"
    assert t.headers == ["Vendor", "Invoice", "Amount"]
    assert t.rows == [
        ["Acme", "12345", "500.00"],
        ["Bosch", "12346", "1200.00"],
    ]


@pytest.mark.unit
def test_html_table_without_thead_falls_back_to_first_row_as_headers() -> None:
    frag = """
    <table>
      <tr><td>a</td><td>b</td></tr>
      <tr><td>1</td><td>2</td></tr>
      <tr><td>3</td><td>4</td></tr>
    </table>
    """
    r = extract(_html(frag), mime="text/html")
    assert r.content.tables is not None
    t = r.content.tables[0]
    assert t.headers == ["a", "b"]
    assert t.rows == [["1", "2"], ["3", "4"]]


@pytest.mark.unit
def test_html_multiple_tables_get_indexed_dom_hints() -> None:
    frag = """
    <table><tr><th>h1</th></tr><tr><td>v1</td></tr></table>
    <table><tr><th>h2</th></tr><tr><td>v2</td></tr></table>
    """
    r = extract(_html(frag), mime="text/html")
    assert r.content.tables is not None
    hints = [t.client_table_hint for t in r.content.tables]
    assert hints == ["html_dom_t0", "html_dom_t1"]


@pytest.mark.unit
def test_html_short_row_is_padded() -> None:
    frag = """
    <table>
      <thead><tr><th>a</th><th>b</th><th>c</th></tr></thead>
      <tbody><tr><td>1</td></tr></tbody>
    </table>
    """
    r = extract(_html(frag), mime="text/html")
    assert r.content.tables is not None
    t = r.content.tables[0]
    assert t.rows == [["1", "", ""]]


@pytest.mark.unit
def test_html_oversized_cell_truncated() -> None:
    frag = f"""
    <table>
      <thead><tr><th>h</th></tr></thead>
      <tbody><tr><td>{"x" * 2000}</td></tr></tbody>
    </table>
    """
    r = extract(_html(frag), mime="text/html")
    assert r.content.tables is not None
    t = r.content.tables[0]
    assert len(t.rows[0][0]) == 1024
    assert any("truncated" in w for w in r.warnings)


@pytest.mark.unit
def test_html_empty_table_skipped() -> None:
    """Table with no data rows produces no entry."""
    frag = "<table><thead><tr><th>h</th></tr></thead></table>"
    r = extract(_html(frag), mime="text/html")
    assert r.content.tables is None
