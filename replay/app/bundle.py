"""Portable, fixed-instant offline replay bundle (ZIP).

An export is the *transitive closure* of the entry page at one fixed cutoff:

* every reachable page/resource is selected with the same rule the online
  replay uses ("newest capture not later than the cutoff"), so removed
  pages can never resurrect newer content and every included version
  belongs to the same historical instant;
* pages are run through the very same whitelist sanitizer + reference
  mapper as the online viewer — ``<base>`` is resolved then removed, and
  every link/image that has a same-cutoff capture is rewritten to a
  *relative in-archive path* (nothing in a cleaned page can jump back to
  the live site);
* cyclic references and shared resources (one logo used by many pages,
  byte-identical pages) produce exactly one file;
* references with no usable capture are turned into the same inert inline
  gap markers the viewer uses and are additionally declared in
  ``manifest.json`` with the reason they are absent;
* the export is all-or-nothing (a bad entry/cutoff raises before any ZIP
  exists), never mutates the loaded archive, and is byte-for-byte
  reproducible for the same archive/entry/cutoff.
"""

from __future__ import annotations

import hashlib
import io
import json
import posixpath
import zipfile
from dataclasses import dataclass, field

from .rewriter import (
    HTML_CONTENT_TYPE,
    PNG_CONTENT_TYPE,
    Ref,
    map_references,
    sanitize_html,
)
from .storage import _parse_cutoff
from .validate import validate_target_uri

BUNDLE_FORMAT = "fixed-instant-replay-bundle/1"
ENTRY_FILE = "index.html"
MANIFEST_FILE = "manifest.json"
ASSET_DIR = "assets"
PAGE_DIR = "pages"

# Gap status codes written into the manifest.
GAP_SCHEME = "non-http-scheme"
GAP_NOT_ARCHIVED = "not-archived"
GAP_AFTER_CUTOFF = "only-captured-after-cutoff"
GAP_WRONG_TYPE = "wrong-media-type"
GAP_EMPTY = "empty-reference"


class BundleError(ValueError):
    """The bundle could not be built (invalid entry/cutoff, not HTML, ...)."""


@dataclass
class _Node:
    uri: str
    record: object
    is_page: bool
    refs: list[Ref] = field(default_factory=list)
    base_href: str | None = None
    group: str | None = None  # page group id, pages only


@dataclass
class _Group:
    key: str
    members: list[_Node]
    is_entry_group: bool
    output_path: str
    output_bytes: bytes | None = None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _relpath(from_dir: str, to_path: str) -> str:
    """Relative path from a file in directory ``from_dir`` to ``to_path``.

    Directories are root-relative (``""`` for the bundle root), so the same
    routine works for the root ``index.html`` and for files under
    ``pages/`` / ``assets/``.
    """
    base = "/" + (from_dir + "/" if from_dir else "")
    return posixpath.relpath("/" + to_path, base)


def _gap_status(ref: Ref) -> str:
    gap = ref.gap
    if gap.reason == "empty":
        return GAP_EMPTY
    if gap.reason == "scheme":
        return GAP_SCHEME
    if gap.reason == "wrong-type":
        return GAP_WRONG_TYPE
    if gap.has_capture:
        return GAP_AFTER_CUTOFF
    return GAP_NOT_ARCHIVED


def _gap_id(ref: Ref) -> str:
    digest = hashlib.sha256(
        f"{ref.kind}\0{_gap_status(ref)}\0{ref.uri or ''}".encode()
    ).hexdigest()
    return f"g-{digest[:16]}"


# ---------------------------------------------------------------------------
# fixed-cutoff closure
# ---------------------------------------------------------------------------


def _plan(archive, entry_uri: str, cutoff_dt) -> tuple[_Node, dict[str, _Node]]:
    """Resolve the same-cutoff transitive closure reachable from ``entry``."""
    lookup = lambda u: archive.lookup_at(u, cutoff_dt)
    all_uris = {r.target_uri for r in archive.all_records()}
    has_any = lambda u: u in all_uris

    entry_rec = lookup(entry_uri)
    if entry_rec is None:
        raise BundleError("entry has no capture at or before the cutoff")
    if entry_rec.content_type != HTML_CONTENT_TYPE:
        raise BundleError("entry capture is not archived UTF-8 HTML")
    entry = _Node(entry_uri, entry_rec, True)
    nodes: dict[str, _Node] = {entry_uri: entry}

    queue: list[str] = [entry_uri]
    head = 0
    while head < len(queue):
        uri = queue[head]
        head += 1
        node = nodes[uri]
        if not node.is_page:
            continue
        page = sanitize_html(node.record.body, uri)
        node.base_href = page.base_href
        # Discovery renders no markup (map_url returns ""); only refs/gaps
        # are consumed to grow the closure.
        result = map_references(page, lookup, has_any, lambda _ref, _rec: "")
        node.refs = result.refs
        for ref in result.refs:
            target = ref.record
            if target is None:
                continue
            # Only references whose cutoff-selected media type matches the
            # element kind belong to the offline closure (a link to a PNG
            # and an <img> pointing at HTML are rendered as gaps, not files).
            if (
                ref.kind == "link"
                and target.content_type != HTML_CONTENT_TYPE
                or ref.kind == "image"
                and target.content_type != PNG_CONTENT_TYPE
            ):
                continue
            if ref.uri not in nodes:
                nodes[ref.uri] = _Node(
                    ref.uri, target, target.content_type == HTML_CONTENT_TYPE
                )
                queue.append(ref.uri)
    return entry, nodes


# ---------------------------------------------------------------------------
# grouping: cycles/shared pages/assets -> one file each
# ---------------------------------------------------------------------------


def _page_group_key(node: _Node, is_entry: bool) -> str:
    """Identity of the file a cleaned page is rendered into.

    Two URIs share a file when their cleaned output is byte-identical under
    the flat layout: same raw body, same effective base and same set of
    referenced target URIs (mapped refs and gaps).  The entry is always
    isolated so it can own ``index.html``.
    """
    h = hashlib.sha256()
    h.update(b"page\0")
    h.update(b"entry\0" if is_entry else b"sub\0")
    h.update(_sha256(node.record.body).encode())
    h.update(b"\0")
    h.update((node.base_href or "").encode("utf-8"))
    h.update(b"\0")
    ref_uris = sorted({r.uri or "" for r in node.refs})
    h.update("␟".join(ref_uris).encode("utf-8"))
    return h.hexdigest()


def _assign_groups(entry: _Node, nodes: dict[str, _Node]) -> list[_Group]:
    groups_by_key: dict[str, _Group] = {}
    for uri in sorted(nodes):
        node = nodes[uri]
        if not node.is_page:
            continue
        is_entry = node is entry
        key = _page_group_key(node, is_entry)
        node.group = key
        g = groups_by_key.get(key)
        if g is None:
            g = _Group(
                key=key,
                members=[],
                is_entry_group=is_entry,
                output_path=ENTRY_FILE if is_entry else "",
            )
            groups_by_key[key] = g
        g.members.append(node)

    sub_groups = sorted(
        (g for g in groups_by_key.values() if not g.is_entry_group),
        key=lambda g: min(m.uri for m in g.members),
    )
    for g in sub_groups:
        g.output_path = f"{PAGE_DIR}/{g.key[:16]}.html"
    return sorted(groups_by_key.values(), key=lambda g: g.output_path)


def _group_of(groups: list[_Group], node: _Node) -> _Group:
    return next(g for g in groups if g.key == node.group)


def _asset_path(rec) -> str:
    # Content addressed: identical payload bytes share one file.
    return f"{ASSET_DIR}/{_sha256(rec.body)[:32]}.png"


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _render_groups(
    groups: list[_Group],
    nodes: dict[str, _Node],
    entry: _Node,
    archive,
    cutoff_dt,
) -> None:
    lookup = lambda u: archive.lookup_at(u, cutoff_dt)
    all_uris = {r.target_uri for r in archive.all_records()}
    has_any = lambda u: u in all_uris

    page_path = {
        uri: _group_of(groups, node).output_path
        for uri, node in nodes.items()
        if node.is_page
    }
    asset_path = {
        uri: _asset_path(node.record)
        for uri, node in nodes.items()
        if not node.is_page
    }

    current: dict[str, _Node] = {"node": entry}

    def map_url(ref: Ref, _rec) -> str:
        source = page_path[current["node"].uri]
        from_dir = "" if source == ENTRY_FILE else PAGE_DIR
        to = asset_path[ref.uri] if ref.kind == "image" else page_path[ref.uri]
        return _relpath(from_dir, to)

    for g in groups:
        member = min(g.members, key=lambda n: n.uri)
        current["node"] = member
        page = sanitize_html(member.record.body, member.uri)
        result = map_references(page, lookup, has_any, map_url)
        # Belt-and-braces: the render pass must classify each reference the
        # same way the discovery pass did (same cutoff selection).
        discovered = [
            (r.uri, r.kind, r.record is not None, r.gap.reason if r.gap else None)
            for r in member.refs
        ]
        rendered = [
            (r.uri, r.kind, r.record is not None, r.gap.reason if r.gap else None)
            for r in result.refs
        ]
        if discovered != rendered:
            raise BundleError(
                "internal error: reference classification drifted between passes"
            )
        g.output_bytes = result.html.encode("utf-8")


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------


def _build_manifest(
    *,
    entry: _Node,
    nodes: dict[str, _Node],
    groups: list[_Group],
    cutoff_z: str,
) -> tuple[dict, list[tuple[str, bytes]]]:
    files: list[tuple[str, bytes]] = []

    # Page group files.
    resources: list[dict] = []
    for g in groups:
        files.append((g.output_path, g.output_bytes))
        digest = _sha256(g.output_bytes)
        for uri in sorted(m.uri for m in g.members):
            node = next(n for n in g.members if n.uri == uri)
            resources.append(
                {
                    "uri": uri,
                    "record_id": node.record.record_id,
                    "captured_at": node.record.date_iso,
                    "media_type": HTML_CONTENT_TYPE,
                    "file": g.output_path,
                    "file_sha256": digest,
                    "effective_base_href": node.base_href,
                }
            )

    # Asset files (content addressed -> identical bytes write once).
    written: set[str] = set()
    for uri in sorted(u for u, n in nodes.items() if not n.is_page):
        node = nodes[uri]
        path = _asset_path(node.record)
        if path not in written:
            files.append((path, node.record.body))
            written.add(path)
        resources.append(
            {
                "uri": uri,
                "record_id": node.record.record_id,
                "captured_at": node.record.date_iso,
                "media_type": PNG_CONTENT_TYPE,
                "file": path,
                "file_sha256": _sha256(node.record.body),
            }
        )
    resources.sort(key=lambda r: r["uri"])

    # Global, de-duplicated gap catalogue plus per-page raw references.
    catalogue: dict[str, dict] = {}
    gap_references: list[dict] = []
    for node in sorted((n for n in nodes.values() if n.is_page), key=lambda n: n.uri):
        for ref in node.refs:
            if ref.gap is None:
                continue
            gid = _gap_id(ref)
            if gid not in catalogue:
                catalogue[gid] = {
                    "id": gid,
                    "reference_kind": ref.kind,
                    "uri": ref.uri,
                    "expected_media_type": (
                        HTML_CONTENT_TYPE if ref.kind == "link" else PNG_CONTENT_TYPE
                    ),
                    "status": _gap_status(ref),
                    "detail": ref.gap.detail,
                    "capture_media_type": ref.gap.capture_content_type,
                    "referenced_by": [],
                }
            if node.uri not in catalogue[gid]["referenced_by"]:
                catalogue[gid]["referenced_by"].append(node.uri)
            gap_references.append(
                {"source_uri": node.uri, "gap_id": gid, "raw_reference": ref.raw}
            )
    gaps = sorted(catalogue.values(), key=lambda g: g["id"])
    for g in gaps:
        g["referenced_by"].sort()
    gap_references.sort(key=lambda x: (x["source_uri"], x["raw_reference"]))

    manifest = {
        "format": BUNDLE_FORMAT,
        "cutoff": cutoff_z,
        "entry_uri": entry.uri,
        "entry_file": ENTRY_FILE,
        "entry_record_id": entry.record.record_id,
        "entry_captured_at": entry.record.date_iso,
        "resource_count": len(resources),
        "file_count": 0,  # filled by caller-independent value below
        "gap_count": len(gaps),
        "resources": resources,
        "gaps": gaps,
        "gap_references": gap_references,
        "notes": [
            "Every resource is the newest capture not later than the cutoff; "
            "opening this bundle never contacts the live site.",
            "Pages use the online replay sanitizer whitelist; <base> is "
            "resolved and removed and links/images are relative in-bundle.",
            "Each gap is a reference with no same-cutoff capture; 'status' "
            "explains why it is absent and a marker stands in for it.",
        ],
    }
    manifest["file_count"] = len({p for p, _ in files}) + 1  # + manifest.json
    return manifest, files


# ---------------------------------------------------------------------------
# deterministic ZIP
# ---------------------------------------------------------------------------


def _write_zip(manifest: dict, files: list[tuple[str, bytes]]) -> bytes:
    payload = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    all_files = sorted(set(files) | {(MANIFEST_FILE, payload)})
    out = io.BytesIO()
    with zipfile.ZipFile(
        out,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as z:
        for path, data in all_files:
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # UNIX, deterministic across platforms
            info.external_attr = (0o644 & 0xFFFF) << 16
            z.writestr(info, data)
    return out.getvalue()


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


class BundleVerificationError(ValueError):
    """A bundle on disk is internally inconsistent or can escape offline."""


def verify_bundle(data: bytes) -> dict:
    """Validate an exported bundle *without* the original archive.

    Checks ZIP structure, deterministic ordering, manifest hashes against
    actual file bytes, entry/resource provenance fields, that every local
    link/image in a cleaned page resolves to a declared file, that no
    reference can escape the bundle (absolute URL, root-escaping or
    ``..`` traversal), that declared gaps match the manifest, and that no
    file is present without a manifest entry (and vice versa).  Returns the
    parsed manifest on success.
    """
    import re
    from html.parser import HTMLParser

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise BundleVerificationError(f"not a zip file: {exc}") from exc
    bad = zf.testzip()
    if bad is not None:
        raise BundleVerificationError(f"corrupt zip member: {bad}")

    names = zf.namelist()
    if names != sorted(names):
        raise BundleVerificationError("zip entries are not sorted")
    if len(names) != len(set(names)):
        raise BundleVerificationError("duplicate zip entries")
    if MANIFEST_FILE not in names or ENTRY_FILE not in names:
        raise BundleVerificationError("bundle lacks index.html or manifest.json")

    # Deterministic archive metadata.
    for info in zf.infolist():
        if info.date_time != (1980, 1, 1, 0, 0, 0):
            raise BundleVerificationError(f"non-fixed zip timestamp: {info.filename}")

    try:
        manifest = json.loads(zf.read(MANIFEST_FILE).decode("utf-8"))
    except Exception as exc:
        raise BundleVerificationError(f"manifest.json unreadable: {exc}") from exc

    for key in (
        "format",
        "cutoff",
        "entry_uri",
        "entry_file",
        "entry_record_id",
        "entry_captured_at",
        "resources",
        "gaps",
        "gap_references",
    ):
        if key not in manifest:
            raise BundleVerificationError(f"manifest missing field: {key}")
    if manifest["format"] != BUNDLE_FORMAT:
        raise BundleVerificationError(f"unknown bundle format: {manifest['format']}")
    if manifest["entry_file"] != ENTRY_FILE:
        raise BundleVerificationError("entry_file must be index.html")

    resources = manifest["resources"]
    uris = [r.get("uri") for r in resources]
    if len(uris) != len(set(uris)):
        raise BundleVerificationError("duplicate resource URIs in manifest")

    # Every declared file exists with matching sha-256; every zip member is
    # declared exactly once.
    declared: dict[str, str] = {}
    for r in resources:
        for key in (
            "uri",
            "record_id",
            "captured_at",
            "media_type",
            "file",
            "file_sha256",
        ):
            if key not in r:
                raise BundleVerificationError(f"resource missing field {key}: {r}")
        if r["file"] not in names:
            raise BundleVerificationError(f"missing packaged file: {r['file']}")
        actual = _sha256(zf.read(r["file"]))
        if actual != r["file_sha256"]:
            raise BundleVerificationError(f"sha256 mismatch for {r['file']}")
        # Same file must always carry the same digest (shared-file rule).
        if r["file"] in declared and declared[r["file"]] != actual:
            raise BundleVerificationError(f"file hashed twice differently: {r['file']}")
        declared[r["file"]] = actual
        if r["captured_at"] > manifest["cutoff"]:
            raise BundleVerificationError(
                f"resource {r['uri']} captured after the cutoff"
            )

    undeclared = set(names) - {MANIFEST_FILE} - set(declared)
    if undeclared:
        raise BundleVerificationError(f"files without manifest entries: {sorted(undeclared)}")

    entry_res = next(
        (r for r in resources if r["uri"] == manifest["entry_uri"]), None
    )
    if entry_res is None or entry_res["file"] != ENTRY_FILE:
        raise BundleVerificationError("entry URI is not packaged as index.html")
    if entry_res["record_id"] != manifest["entry_record_id"]:
        raise BundleVerificationError("entry record id inconsistent")
    if entry_res["captured_at"] != manifest["entry_captured_at"]:
        raise BundleVerificationError("entry capture date inconsistent")

    file_uris: dict[str, set[str]] = {}
    for r in resources:
        file_uris.setdefault(r["file"], set()).add(r["uri"])

    # Walk local references in every packaged HTML page.
    abs_url = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.IGNORECASE)
    root_escaping = re.compile(r"^/")

    class _RefGrabber(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.links: list[tuple[str, str]] = []  # (attr, value)

        def handle_starttag(self, tag, attrs):
            for k, v in attrs:
                if v is None:
                    continue
                if (tag == "a" and k == "href") or (tag == "img" and k == "src"):
                    self.links.append((k, v.strip()))

    page_files = {
        r["file"]
        for r in resources
        if r["media_type"] == HTML_CONTENT_TYPE
    }
    targets: dict[str, set[str]] = {}
    for path in sorted(page_files):
        html_bytes = zf.read(path)
        try:
            html_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BundleVerificationError(f"{path} is not valid UTF-8") from exc
        grabber = _RefGrabber()
        grabber.feed(html_bytes.decode("utf-8"))
        source_dir = "" if path == ENTRY_FILE else posixpath.dirname(path)
        page_targets: set[str] = set()
        for _attr, ref in grabber.links:
            if ref == "" or ref.startswith(("#", "mailto:", "javascript:")):
                continue
            if abs_url.match(ref) or root_escaping.match(ref) or ref.startswith("\\"):
                raise BundleVerificationError(
                    f"{path} contains an escaping reference: {ref}"
                )
            target = posixpath.normpath(posixpath.join(source_dir, ref))
            if target.startswith("..") or target not in declared:
                raise BundleVerificationError(
                    f"{path} references file outside the bundle: {ref}"
                )
            page_targets.add(target)
        targets[path] = page_targets

    # Reachability: every packaged file must be reachable from index.html by
    # following the cleaned local links (the bundle is one closed web).
    reached: set[str] = {ENTRY_FILE}
    stack = [ENTRY_FILE]
    while stack:
        cur = stack.pop()
        for t in targets.get(cur, ()):
            if t not in reached:
                reached.add(t)
                stack.append(t)
    unreachable = set(declared) - reached
    if unreachable:
        raise BundleVerificationError(
            f"files not reachable from the entry: {sorted(unreachable)}"
        )

    # Gap catalogue consistency.
    gap_ids = {g["id"] for g in manifest["gaps"]}
    if len(gap_ids) != len(manifest["gaps"]):
        raise BundleVerificationError("duplicate gap ids")
    for g in manifest["gaps"]:
        for key in ("id", "reference_kind", "status", "referenced_by"):
            if key not in g:
                raise BundleVerificationError(f"gap missing field {key}: {g}")
        for src in g["referenced_by"]:
            if src not in uris:
                raise BundleVerificationError(f"gap {g['id']} references unknown page")
    for gr in manifest["gap_references"]:
        if gr["gap_id"] not in gap_ids:
            raise BundleVerificationError(
                f"gap_reference points at unknown gap {gr['gap_id']}"
            )
        if gr["source_uri"] not in uris:
            raise BundleVerificationError("gap_reference from unknown source page")

    if manifest["resource_count"] != len(resources):
        raise BundleVerificationError("resource_count does not match resources")
    if manifest["gap_count"] != len(manifest["gaps"]):
        raise BundleVerificationError("gap_count does not match gaps")
    if manifest["file_count"] != len(names):
        raise BundleVerificationError("file_count does not match zip members")
    return manifest


def export_bundle(archive, uri, cutoff) -> bytes:
    """Build the offline ZIP bundle for one entry at one cutoff.

    Raises :class:`BundleError` for an invalid URI/cutoff or an entry with
    no same-cutoff HTML capture.  ``archive`` is only ever read.
    """
    try:
        canonical = validate_target_uri(uri)
    except Exception as exc:  # WARCFormatError is-a ValueError
        raise BundleError(f"invalid entry URI: {exc}") from exc
    try:
        cutoff_dt = _parse_cutoff(cutoff)
    except (ValueError, TypeError) as exc:
        raise BundleError(f"invalid cutoff: {exc}") from exc
    cutoff_z = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    entry, nodes = _plan(archive, canonical, cutoff_dt)
    groups = _assign_groups(entry, nodes)
    _render_groups(groups, nodes, entry, archive, cutoff_dt)
    manifest, files = _build_manifest(
        entry=entry, nodes=nodes, groups=groups, cutoff_z=cutoff_z
    )
    return _write_zip(manifest, files)


# ---------------------------------------------------------------------------
# command line: python -m app.bundle export|verify ...
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    import argparse

    from .storage import Archive

    parser = argparse.ArgumentParser(
        prog="app.bundle",
        description="Export / verify fixed-instant offline replay bundles.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    ex = sub.add_parser("export", help="build a bundle from an uncompressed WARC")
    ex.add_argument("warc", help="path to the .warc archive")
    ex.add_argument("--uri", required=True, help="entry page URI")
    ex.add_argument("--cutoff", required=True, help="fixed cutoff, e.g. 2015-10-21T07:30:00Z")
    ex.add_argument("--out", required=True, help="output .zip path")

    vf = sub.add_parser("verify", help="verify a bundle zip without the archive")
    vf.add_argument("zip", help="bundle .zip path")

    args = parser.parse_args(argv)
    if args.cmd == "export":
        archive = Archive()
        archive.load(open(args.warc, "rb").read())
        data = export_bundle(archive, args.uri, args.cutoff)
        with open(args.out, "wb") as fh:
            fh.write(data)
        manifest = verify_bundle(data)
        print(
            f"wrote {args.out}: {manifest['resource_count']} resources, "
            f"{manifest['file_count']} files, {manifest['gap_count']} gaps, "
            f"cutoff {manifest['cutoff']}"
        )
        return 0

    with open(args.zip, "rb") as fh:
        manifest = verify_bundle(fh.read())
    print(
        f"OK: {manifest['entry_uri']} @ {manifest['cutoff']}, "
        f"{manifest['resource_count']} resources, {manifest['gap_count']} gaps"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
