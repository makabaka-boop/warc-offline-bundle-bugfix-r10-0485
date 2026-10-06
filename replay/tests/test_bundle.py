import datetime as dt
import hashlib
import io
import zipfile

import pytest

from app.bundle import MANIFEST_FILE, export_bundle
from app.storage import Archive
from app.warc_parser import WARCFormatError
from tests.warcmaker import PNG_1X1, PNG_2X2, archive, warc_record

D_OLD = dt.datetime(2015, 10, 21, 7, 28, tzinfo=dt.timezone.utc)
D_NEW = dt.datetime(2016, 10, 21, 9, 0, tzinfo=dt.timezone.utc)

ROOT = "http://example.com/"
CONTACT = "http://example.com/contact.html"
SHARED = "http://example.com/assets/shared.png"
MISSING = "http://example.com/missing.png"
UNREACHABLE_PAGE = "http://example.com/unreachable.html"
UNREACHABLE_PNG = "http://example.com/unreachable.png"

ROOT_OLD = (
    b"<html><head><base href='/assets/'></head><body>"
    b"<h1>Old root</h1>"
    b"<a href='contact.html'>contact</a>"
    b"<img src='shared.png' alt='shared'>"
    b"<img src='../missing.png'>"
    b"<img src='http://tracker.example/p.png'>"
    b"<a href='javascript:alert(1)'>script</a>"
    b"</body></html>"
)
ROOT_NEW = b"<html><body><h1>New root</h1></body></html>"
CONTACT_OLD = (
    b"<html><body><h1>Old contact</h1>"
    b"<a href='/'>back to root</a>"
    b"<a href='/'>duplicate cycle link</a>"
    b"<img src='assets/shared.png'>"
    b"<a href='http://other.example/'>external</a>"
    b"</body></html>"
)
CONTACT_NEW = b"<html><body><h1>New contact</h1></body></html>"
UNREACHABLE_HTML = (
    b"<html><body><img src='unreachable.png'></body></html>"
)


def _html(record_id, uri, when, body):
    return warc_record(
        record_id=record_id,
        uri=uri,
        dt=when,
        content_type="text/html; charset=utf-8",
        body=body,
    )


def _png(record_id, uri, when, body=PNG_1X1):
    return warc_record(
        record_id=record_id, uri=uri, dt=when, content_type="image/png", body=body
    )


@pytest.fixture()
def graph_archive():
    store = Archive()
    store.load(
        archive(
            _html("<root-old>", ROOT, D_OLD, ROOT_OLD),
            _html("<root-new>", ROOT, D_NEW, ROOT_NEW),
            _html("<contact-old>", CONTACT, D_OLD, CONTACT_OLD),
            _html("<contact-new>", CONTACT, D_NEW, CONTACT_NEW),
            _png("<shared-old>", SHARED, D_OLD, PNG_1X1),
            _png("<shared-new>", SHARED, D_NEW, PNG_2X2),
            _html("<unreachable>", UNREACHABLE_PAGE, D_OLD, UNREACHABLE_HTML),
            _png("<unreachable-png>", UNREACHABLE_PNG, D_OLD),
        )
    )
    return store


def _zip(data):
    return zipfile.ZipFile(io.BytesIO(data))


def _by_path(payload):
    zf = _zip(payload)
    return {info.filename: zf.read(info.filename) for info in zf.infolist()}


def _manifest(payload):
    import json

    return json.loads(_by_path(payload)[MANIFEST_FILE].decode("utf-8"))


def test_bundle_contains_one_fixed_cutoff_reachable_graph(graph_archive):
    payload = export_bundle(graph_archive, ROOT, "2015-10-21T08:00:00Z")
    files = _by_path(payload)
    manifest = _manifest(payload)

    assert files["index.html"].count(b"Old root") == 1
    included = {r["uri"]: r for r in manifest["resources"]}
    assert set(included) == {ROOT, CONTACT, SHARED}
    assert included[ROOT]["record_id"] == "<root-old>"
    assert included[CONTACT]["record_id"] == "<contact-old>"
    assert included[SHARED]["record_id"] == "<shared-old>"
    assert included[SHARED]["content_type"] == "image/png"
    assert files[included[SHARED]["path"]] == PNG_1X1
    assert UNREACHABLE_PAGE not in included
    assert UNREACHABLE_PNG not in included


def test_cycles_and_shared_resources_have_one_file_and_consistent_paths(graph_archive):
    payload = export_bundle(graph_archive, ROOT, "2015-10-21T08:00:00Z")
    zf = _zip(payload)
    manifest = _manifest(payload)
    root_path = next(r["path"] for r in manifest["resources"] if r["uri"] == ROOT)
    contact_path = next(
        r["path"] for r in manifest["resources"] if r["uri"] == CONTACT
    )
    shared_path = next(r["path"] for r in manifest["resources"] if r["uri"] == SHARED)
    assert root_path == "index.html"

    html_files = [n for n in zf.namelist() if n.endswith(".html")]
    asset_files = [n for n in zf.namelist() if n.endswith(".png")]
    assert html_files == ["index.html", contact_path]
    assert asset_files == [shared_path]

    root_html = zf.read(root_path).decode("utf-8")
    contact_html = zf.read(contact_path).decode("utf-8")
    assert f'href="{contact_path}"' in root_html
    assert f'src="{shared_path}"' in root_html
    assert 'href="index.html"' in contact_html
    assert contact_html.count('href="index.html"') == 2
    assert f'src="{shared_path}"' in contact_html


def test_gaps_are_inert_in_html_and_explained_in_manifest(graph_archive):
    payload = export_bundle(graph_archive, ROOT, "2015-10-21T08:00:00Z")
    files = _by_path(payload)
    manifest = _manifest(payload)
    root_html = files["index.html"].decode("utf-8")

    assert "tracker.example" not in root_html
    assert "javascript:alert" not in root_html
    assert "<base" not in root_html
    assert "other.example" not in files
    assert 'src="http' not in root_html
    assert 'href="http' not in root_html
    assert "data-gap" in root_html

    gap_map = {(g["source_uri"], g["kind"], g["reference"]): g for g in manifest["gaps"]}
    assert gap_map[(ROOT, "image", "../missing.png")]["reason"] == (
        "no capture at or before the cutoff"
    )
    assert gap_map[(ROOT, "image", "http://tracker.example/p.png")]["resolved_uri"] == (
        "http://tracker.example/p.png"
    )
    assert gap_map[(ROOT, "link", "javascript:alert(1)")]["resolved_uri"] is None
    external = gap_map[(CONTACT, "link", "http://other.example/")]
    assert external["reason"] == "no capture at or before the cutoff"


def test_manifest_digests_paths_and_sanitized_files_agree(graph_archive):
    payload = export_bundle(graph_archive, ROOT, "2015-10-21T08:00:00Z")
    files = _by_path(payload)
    for item in _manifest(payload)["resources"]:
        body = files[item["path"]]
        assert hashlib.sha256(body).hexdigest() == item["sha256"]
        assert len(body) == item["bytes"]
        assert item["captured_at"] == "2015-10-21T07:28:00Z"
        if item["path"] != "index.html":
            assert hashlib.sha256(item["uri"].encode()).hexdigest()[:16] in item["path"]


def test_repeated_exports_are_byte_identical(graph_archive):
    first = export_bundle(graph_archive, ROOT, "2015-10-21T08:00:00Z")
    second = export_bundle(graph_archive, ROOT, "2015-10-21T08:00:00Z")
    assert first == second

    infos = _zip(first).infolist()
    assert [info.filename for info in infos][0] == "index.html"
    assert [info.filename for info in infos][-1] == MANIFEST_FILE
    assert {i.date_time for i in infos} == {(1980, 1, 1, 0, 0, 0)}
    assert len({i.CRC for i in infos}) == len(infos)


def test_later_cutoff_selects_newer_records(graph_archive):
    payload = export_bundle(graph_archive, ROOT, "2020-01-01T00:00:00Z")
    manifest = _manifest(payload)
    included = {r["uri"]: r for r in manifest["resources"]}
    assert set(included) == {ROOT}
    assert included[ROOT]["record_id"] == "<root-new>"


def test_invalid_entry_cutoff_and_non_html_entry_are_rejected(graph_archive):
    with pytest.raises(ValueError):
        export_bundle(graph_archive, ROOT, "not-a-time")
    with pytest.raises((ValueError, WARCFormatError)):
        export_bundle(graph_archive, "ftp://example.com/", "2020-01-01T00:00:00Z")
    with pytest.raises(ValueError, match="entry"):
        export_bundle(
            graph_archive, "http://example.com/nope", "2020-01-01T00:00:00Z"
        )
    with pytest.raises(ValueError, match="HTML"):
        export_bundle(graph_archive, SHARED, "2020-01-01T00:00:00Z")
