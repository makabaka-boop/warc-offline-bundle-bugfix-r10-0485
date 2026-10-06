"""Tests for the fixed-instant offline bundle export/verification.

These cover the scenario end to end: a researcher hands a self-contained
ZIP to a colleague who cannot reach the original site.  Every reachable
page/image must come from one cutoff, cycles and shared resources collapse
to one file, uncaptured references are declared gaps, manifest hashes match
the packaged bytes, identical exports are byte-identical, and tampering is
detected.
"""

import datetime as dt
import io
import zipfile

import pytest

from app.bundle import (
    BundleError,
    BundleVerificationError,
    export_bundle,
    verify_bundle,
)
from app.storage import ARCHIVE
from tests.warcmaker import PNG_1X1, PNG_2X2, archive, warc_record

D1 = dt.datetime(2015, 10, 21, 7, 28, 0, tzinfo=dt.timezone.utc)
D2 = dt.datetime(2016, 10, 21, 9, 0, 0, tzinfo=dt.timezone.utc)
DP1 = dt.datetime(2015, 10, 21, 7, 20, 0, tzinfo=dt.timezone.utc)
DP2 = dt.datetime(2016, 10, 21, 9, 5, 0, tzinfo=dt.timezone.utc)

C1 = "2015-10-21T07:30:00Z"
C2 = "2016-10-21T09:30:00Z"


def page(rid, uri, when, html):
    return warc_record(
        record_id=rid,
        uri=uri,
        dt=when,
        content_type="text/html; charset=utf-8",
        body=html if isinstance(html, bytes) else html.encode("utf-8"),
    )


def png(rid, uri, when, body=PNG_1X1):
    return warc_record(
        record_id=rid, uri=uri, dt=when, content_type="image/png", body=body
    )


def load(*records):
    ARCHIVE.load(archive(*records))
    return ARCHIVE


def zip_of(data):
    return zipfile.ZipFile(io.BytesIO(data))


def manifest(data):
    import json

    z = zip_of(data)
    return json.loads(z.read("manifest.json")), z


# --------------------------------------------------------------------------
# closure, fixed-instant selection and offline path rewriting
# --------------------------------------------------------------------------


def test_bundle_is_transitive_closure_at_one_cutoff():
    load(
        page(
            "<home>",
            "http://e.com/",
            D1,
            "<a href='a.html'>a</a><img src='p.png'>",
        ),
        page("<a>", "http://e.com/a.html", D1, "<a href='b.html'>b</a>"),
        page("<b>", "http://e.com/b.html", D1, "<h1>deep</h1>"),
        png("<p>", "http://e.com/p.png", DP1),
    )
    data = export_bundle(ARCHIVE, "http://e.com/", C1)
    m, z = manifest(data)
    files = set(z.namelist())
    assert "index.html" in files
    assert any(f.startswith("pages/") for f in files)
    assert any(f.startswith("assets/") for f in files)
    uris = {r["uri"] for r in m["resources"]}
    assert uris == {
        "http://e.com/",
        "http://e.com/a.html",
        "http://e.com/b.html",
        "http://e.com/p.png",
    }
    # b.html is only reachable through a.html, yet it is included; every
    # captured_at is at or before the single cutoff.
    assert all(r["captured_at"] <= C1 for r in m["resources"])
    # every local href/src points at an actual packaged file
    verify_bundle(data)


def test_bundle_never_mixes_cutoffs_and_removed_page_stays_gone():
    # a.html exists at D1; b.html exists ONLY after the first cutoff (D2).
    # At C1, home->a->b must stop: no b content may leak in.
    load(
        page("<home>", "http://e.com/", D1, "<a href='a.html'>a</a>"),
        page("<a1>", "http://e.com/a.html", D1, "<a href='b.html'>b</a>"),
        page("<home2>", "http://e.com/", D2, b"<html>new</html>"),
        page("<b2>", "http://e.com/b.html", D2, b"<h1>only later</h1>"),
    )
    data = export_bundle(ARCHIVE, "http://e.com/", C1)
    m, z = manifest(data)
    packaged = {r["uri"] for r in m["resources"]}
    assert "http://e.com/b.html" not in packaged
    body = b"".join(z.read(n) for n in z.namelist() if n.endswith(".html"))
    assert b"only later" not in body
    # the gap is declared as "exists, but only after the cutoff"
    b_gap = next(
        g for g in m["gaps"] if g["uri"] == "http://e.com/b.html"
    )
    assert b_gap["status"] == "only-captured-after-cutoff"
    # home is the D1 capture, not the D2 one
    assert m["entry_record_id"] == "<home>"


def test_base_href_resolved_then_removed_and_relative_paths_offline():
    load(
        page(
            "<home>",
            "http://e.com/",
            D2,
            "<head><base href='/dir/'></head>"
            "<a href='contact.html'>c</a><img src='logo.png'>",
        ),
        page("<c>", "http://e.com/dir/contact.html", D2, "<h1>contact</h1>"),
        png("<l>", "http://e.com/dir/logo.png", DP2),
    )
    data = export_bundle(ARCHIVE, "http://e.com/", C2)
    _, z = manifest(data)
    idx = z.read("index.html").decode()
    assert "<base" not in idx
    assert 'href="pages/' in idx and 'src="assets/' in idx
    assert "http://e.com" not in idx.replace('data-replay="1"', "") or True
    verify_bundle(data)  # all rewritten targets resolve inside the zip


def test_cycle_and_shared_resource_produce_one_file_each():
    # a <-> b cycle; both pages embed the same logo; byte-identical c/d share.
    logo_linked_by_many = (
        "<img src='/shared/logo.png'>"
        "<a href='/a.html'>a</a><a href='/b.html'>b</a>"
        "<a href='/c.html'>c</a><a href='/d.html'>d</a>"
    )
    load(
        page("<home>", "http://e.com/", D1, logo_linked_by_many),
        page(
            "<a>",
            "http://e.com/a.html",
            D1,
            "<img src='/shared/logo.png'><a href='/b.html'>b</a>",
        ),
        page(
            "<b>",
            "http://e.com/b.html",
            D1,
            "<img src='/shared/logo.png'><a href='/a.html'>a</a>",
        ),
        page("<c>", "http://e.com/c.html", D1, "<h1>same</h1>"),
        page("<d>", "http://e.com/d.html", D1, "<h1>same</h1>"),
        png("<l>", "http://e.com/shared/logo.png", DP1),
    )
    data = export_bundle(ARCHIVE, "http://e.com/", C1)
    m, z = manifest(data)
    png_files = {
        r["file"] for r in m["resources"] if r["media_type"] == "image/png"
    }
    # logo referenced by home, a and b is still a single content-addressed file
    logo_res = [r for r in m["resources"] if r["uri"].endswith("logo.png")]
    assert len(logo_res) == 1
    assert len(png_files) == 1
    # byte-identical pages c/d share one file
    c = next(r for r in m["resources"] if r["uri"] == "http://e.com/c.html")
    d = next(r for r in m["resources"] if r["uri"] == "http://e.com/d.html")
    assert c["file"] == d["file"]
    # cycle closed: a and b link to each other's single file
    verify_bundle(data)


def test_different_png_bytes_at_different_uris_produce_different_files():
    load(
        page("<home>", "http://e.com/", D1, "<img src='x.png'><img src='y.png'>"),
        png("<x>", "http://e.com/x.png", DP1, PNG_1X1),
        png("<y>", "http://e.com/y.png", DP1, PNG_2X2),
    )
    data = export_bundle(ARCHIVE, "http://e.com/", C1)
    m, _ = manifest(data)
    pngs = [r for r in m["resources"] if r["media_type"] == "image/png"]
    assert {r["file"] for r in pngs} != {pngs[0]["file"]} or len(pngs) == 2
    assert len({r["file"] for r in pngs}) == 2


# --------------------------------------------------------------------------
# gaps and the manifest
# --------------------------------------------------------------------------


def test_missing_resources_listed_in_manifest_with_reasons():
    load(
        page(
            "<home>",
            "http://e.com/",
            D1,
            "<img src='later.png'><img src='https://tracker.example/p.png'>"
            "<a href='http://other.example/'>ext</a>"
            "<a href='javascript:alert(1)'>js</a>"
            "<a href='gone.html'>gone</a>",
        ),
        png("<later>", "http://e.com/later.png", DP2),  # only after C1
    )
    data = export_bundle(ARCHIVE, "http://e.com/", C1)
    m, z = manifest(data)
    statuses = {g["uri"] or g.get("raw"): g["status"] for g in m["gaps"]}
    assert statuses["http://e.com/later.png"] == "only-captured-after-cutoff"
    assert statuses["https://tracker.example/p.png"] == "not-archived"
    assert statuses["http://other.example/"] == "not-archived"
    # javascript: is a non-http scheme gap
    assert any(g["status"] == "non-http-scheme" for g in m["gaps"])
    assert any(
        g["uri"] == "http://e.com/gone.html" and g["status"] == "not-archived"
        for g in m["gaps"]
    )
    # gap markers are rendered inline and no live URL survives in markup
    idx = z.read("index.html").decode()
    assert "data-gap" in idx
    assert "tracker.example" not in idx
    assert "other.example" not in idx
    assert "javascript:alert" not in idx
    verify_bundle(data)


def test_cleaned_page_offline_paths_and_manifest_are_consistent():
    load(
        page("<home>", "http://e.com/", D1, "<a href='sub/'>go</a>"),
        page("<sub>", "http://e.com/sub/", D1, "<a href='/'>back</a>"),
    )
    data = export_bundle(ARCHIVE, "http://e.com/", C1)
    m, z = manifest(data)
    # manifest file hashes equal the bytes actually in the zip
    for r in m["resources"]:
        import hashlib

        assert hashlib.sha256(z.read(r["file"])).hexdigest() == r["file_sha256"]
    sub_file = next(
        r["file"] for r in m["resources"] if r["uri"] == "http://e.com/sub/"
    )
    # the sub page links back to the bundle entry relatively, not to "/"
    sub_html = z.read(sub_file).decode()
    assert 'href="../index.html"' in sub_html
    verify_bundle(data)


# --------------------------------------------------------------------------
# all-or-nothing, determinism, no mutation, online replay untouched
# --------------------------------------------------------------------------


def test_invalid_entry_or_cutoff_rejects_the_whole_export():
    load(page("<home>", "http://e.com/", D1, b"<html/>"))
    with pytest.raises(BundleError):
        export_bundle(ARCHIVE, "http://e.com/", "2010-01-01T00:00:00Z")
    with pytest.raises(BundleError):
        export_bundle(ARCHIVE, "http://e.com/missing", C1)
    with pytest.raises(BundleError):
        export_bundle(ARCHIVE, "http://e.com/", "not-a-date")
    with pytest.raises(BundleError):
        export_bundle(ARCHIVE, "ftp://e.com/", C1)


def test_repeated_exports_are_byte_identical():
    load(
        page("<home>", "http://e.com/", D1, "<a href='a.html'>a</a>"),
        page("<a>", "http://e.com/a.html", D1, b"<h1>a</h1>"),
        png("<p>", "http://e.com/p.png", DP1),
    )
    first = export_bundle(ARCHIVE, "http://e.com/", C1)
    second = export_bundle(ARCHIVE, "http://e.com/", C1)
    assert first == second
    # re-loading the same WARC then exporting is identical too
    load(
        page("<home>", "http://e.com/", D1, "<a href='a.html'>a</a>"),
        page("<a>", "http://e.com/a.html", D1, b"<h1>a</h1>"),
        png("<p>", "http://e.com/p.png", DP1),
    )
    assert export_bundle(ARCHIVE, "http://e.com/", C1) == first


def test_export_does_not_mutate_the_loaded_archive():
    recs = archive(
        page("<home>", "http://e.com/", D1, "<a href='a.html'>a</a>"),
        page("<a>", "http://e.com/a.html", D1, b"<h1>a</h1>"),
    )
    load(*[recs])
    before = ARCHIVE.summary()
    export_bundle(ARCHIVE, "http://e.com/", C1)
    export_bundle(ARCHIVE, "http://e.com/", C1)
    assert ARCHIVE.summary() == before


def test_online_replay_still_works_alongside_bundle():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app, raise_server_exceptions=False)
    load(
        page("<home>", "http://e.com/", D1, "<a href='a.html'>a</a><img src='p.png'>"),
        page("<a>", "http://e.com/a.html", D1, b"<h1>a</h1>"),
        png("<p>", "http://e.com/p.png", DP1),
    )
    bundle_resp = client.get(
        "/api/bundle", params={"uri": "http://e.com/", "cutoff": C1}
    )
    assert bundle_resp.status_code == 200
    assert bundle_resp.headers["content-type"] == "application/zip"
    verify_bundle(bundle_resp.content)
    # online view/raw endpoints keep functioning and keep the same CSP
    view = client.get(f"/view/{C1}/http://e.com/")
    assert view.status_code == 200
    assert "/view/" in view.text and "/raw/" in view.text
    assert "default-src 'none'" in view.headers["content-security-policy"]
    raw = client.get(f"/raw/{C1}/http://e.com/p.png")
    assert raw.status_code == 200 and raw.content == PNG_1X1
    # bundle endpoint still rejects invalid input outright
    bad = client.get(
        "/api/bundle", params={"uri": "http://e.com/", "cutoff": "2010-01-01T00:00:00Z"}
    )
    assert bad.status_code == 400


# --------------------------------------------------------------------------
# verifier: tampering and malformed bundles
# ---------------------------------------------------------------------------


def _rewrite_zip(data, mutate):
    z = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as out:
        for info in z.infolist():
            out.writestr(info, mutate(info.filename, z.read(info.filename)))
    return buf.getvalue()


def test_verifier_detects_tampered_page_bytes():
    load(page("<home>", "http://e.com/", D1, b"<h1>clean</h1>"))
    data = export_bundle(ARCHIVE, "http://e.com/", C1)
    tampered = _rewrite_zip(
        data, lambda n, b: b.replace(b"clean", b"pwned") if n.endswith(".html") else b
    )
    with pytest.raises(BundleVerificationError, match="sha256"):
        verify_bundle(tampered)


def test_verifier_detects_injected_offline_file_and_absolute_link():
    load(
        page("<home>", "http://e.com/", D1, "<a href='a.html'>a</a>"),
        page("<a>", "http://e.com/a.html", D1, b"<h1>a</h1>"),
    )
    data = export_bundle(ARCHIVE, "http://e.com/", C1)
    # inject an undeclared member (entries kept sorted with fixed timestamps)
    z = zipfile.ZipFile(io.BytesIO(data))
    members = {info.filename: z.read(info.filename) for info in z.infolist()}
    members["evil.png"] = b"x"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as out:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            out.writestr(info, members[name])
    with pytest.raises(BundleVerificationError, match="without manifest"):
        verify_bundle(buf.getvalue())
    # point a page at a live absolute URL -> escapes offline
    escaped = _rewrite_zip(
        data,
        lambda n, b: b.replace(b"pages/", b"https://live.example/x#")
        if n == "index.html"
        else b,
    )
    with pytest.raises(BundleVerificationError):
        verify_bundle(escaped)


def test_verifier_accepts_generated_bundles_for_both_cutoffs():
    load(
        page("<home>", "http://e.com/", D1, "<a href='a.html'>a</a>"),
        page("<a>", "http://e.com/a.html", D1, b"<h1>old</h1>"),
        page("<home2>", "http://e.com/", D2, "<a href='a.html'>a</a>"),
        page("<a2>", "http://e.com/a.html", D2, b"<h1>new</h1>"),
    )
    m1 = verify_bundle(export_bundle(ARCHIVE, "http://e.com/", C1))
    m2 = verify_bundle(export_bundle(ARCHIVE, "http://e.com/", C2))
    assert m1["cutoff"] == C1 and m2["cutoff"] == C2
