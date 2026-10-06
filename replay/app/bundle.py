"""Deterministic, portable fixed-instant archive bundle.

The bundle is produced entirely from records selected at one cutoff.  HTML is
sanitized with the same whitelist used by online replay, then links and images
are rewritten to flat relative paths inside the ZIP.  A reachability walk from
the entry includes linked HTML and shared images; cycles do not duplicate work
or files, and unresolved references remain inline gaps plus manifest entries.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections import deque
from dataclasses import dataclass

from .rewriter import ResourceReference, sanitize_and_rewrite
from .storage import _parse_cutoff, record_datetime
from .validate import validate_target_uri

HTML_TYPE = "text/html; charset=utf-8"
PNG_TYPE = "image/png"

ENTRY_FILE = "index.html"
MANIFEST_FILE = "manifest.json"
_BUNDLE_VERSION = 1


@dataclass(frozen=True)
class _Gap:
    source_uri: str
    kind: str
    raw_reference: str
    resolved_uri: str | None
    reason: str


def _uri_digest(uri: str) -> str:
    return hashlib.sha256(uri.encode("utf-8")).hexdigest()[:16]


def _file_path(uri: str, content_type: str) -> str:
    prefix = "page" if content_type == HTML_TYPE else "asset"
    extension = ".html" if content_type == HTML_TYPE else ".png"
    return f"{prefix}-{_uri_digest(uri)}{extension}"


def _gap_reason(
    kind: str, resolved: str | None, target, expected_type: str
) -> str:
    if resolved is None:
        return "non-http(s) or unresolvable reference"
    if target is None:
        return "no capture at or before the cutoff"
    expected = HTML_TYPE if kind == "link" else PNG_TYPE
    if target.content_type != expected:
        return "captured content type is not " + expected_type
    return "included"


def _snapshot_at(records, cutoff_dt) -> dict[str, object]:
    selected: dict[str, object] = {}
    for record in records:
        when = record_datetime(record)
        if when <= cutoff_dt and (
            (uri := record.target_uri) not in selected
            or when > record_datetime(selected[uri])
        ):
            selected[uri] = record
    return selected


def export_bundle(archive, uri, cutoff):
    """Return deterministic ZIP bytes for the reachable fixed-cutoff graph."""
    try:
        entry_uri = validate_target_uri(uri)
        cutoff_dt = _parse_cutoff(cutoff)
    except Exception as exc:
        raise ValueError(str(exc)) from exc

    # Freeze one record per URI at the cutoff before traversal.  This keeps a
    # concurrent import from mixing versions while the bundle is being built.
    records_at_cutoff = _snapshot_at(archive.all_records(), cutoff_dt)
    entry = records_at_cutoff.get(entry_uri)
    if entry is None:
        raise ValueError("entry has no capture at or before the cutoff")
    if entry.content_type != HTML_TYPE:
        raise ValueError("bundle entry must be archived UTF-8 HTML")

    cutoff_iso = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    paths: dict[str, str] = {entry_uri: ENTRY_FILE}
    records: dict[str, object] = {}
    outputs: dict[str, bytes] = {}
    gaps: dict[tuple[str, str, str], _Gap] = {}
    queued: set[str] = {entry_uri}
    queue: deque[str] = deque([entry_uri])

    def lookup(candidate: str):
        return records_at_cutoff.get(candidate)

    while queue:
        current_uri = queue.popleft()
        record = lookup(current_uri)
        # queued URIs have already passed these checks; keeping the guards
        # makes the traversal's fixed-instant invariant explicit.
        if record is None or record.content_type != HTML_TYPE:
            continue

        def offline_url(target_uri: str, _cutoff: str, *, raw: bool) -> str:
            expected = PNG_TYPE if raw else HTML_TYPE
            target = lookup(target_uri)
            if target is None or target.content_type != expected:
                return ""
            # Assign the stable path while sanitizing the document; reference
            # processing below is responsible for enqueuing or storing it.
            return paths.setdefault(
                target_uri, _file_path(target_uri, target.content_type)
            )

        result = sanitize_and_rewrite(
            record.body,
            current_uri,
            cutoff_iso,
            lookup,
            offline_url,
        )
        outputs[current_uri] = result.html.encode("utf-8")
        records[current_uri] = record

        for ref in result.references:
            target = lookup(ref.resolved) if ref.resolved is not None else None
            expected_label = "archived HTML" if ref.kind == "link" else "PNG"
            included = target is not None and (
                target.content_type
                == (HTML_TYPE if ref.kind == "link" else PNG_TYPE)
            )
            if not included:
                reason = _gap_reason(
                    ref.kind, ref.resolved, target, expected_label
                )
                gaps[(current_uri, ref.kind, ref.raw)] = _Gap(
                    current_uri,
                    ref.kind,
                    ref.raw,
                    ref.resolved,
                    reason,
                )
                continue

            if ref.resolved not in paths:
                paths[ref.resolved] = _file_path(ref.resolved, target.content_type)
            if ref.kind == "image":
                if ref.resolved not in records:
                    records[ref.resolved] = target
                    outputs[ref.resolved] = target.body
            elif ref.resolved not in queued:
                queued.add(ref.resolved)
                queue.append(ref.resolved)

    manifest_resources = []
    for resource_uri in sorted(records):
        record = records[resource_uri]
        body = outputs[resource_uri]
        manifest_resources.append(
            {
                "uri": resource_uri,
                "record_id": record.record_id,
                "captured_at": record.date_iso,
                "content_type": record.content_type,
                "media_type": record.content_type,
                "path": paths[resource_uri],
                "sha256": hashlib.sha256(body).hexdigest(),
                "source_sha256": hashlib.sha256(record.body).hexdigest(),
                "bytes": len(body),
            }
        )

    manifest_gaps = [
        {
            "source_uri": gap.source_uri,
            "kind": gap.kind,
            "reference": gap.raw_reference,
            "resolved_uri": gap.resolved_uri,
            "reason": gap.reason,
        }
        for gap in (gaps[key] for key in sorted(gaps))
    ]

    manifest = {
        "bundle_version": _BUNDLE_VERSION,
        "cutoff": cutoff_iso,
        "fixed_instant": cutoff_iso,
        "entry_uri": entry_uri,
        "entry_record_id": entry.record_id,
        "entry_file": ENTRY_FILE,
        "resources": manifest_resources,
        "gaps": manifest_gaps,
    }
    manifest_bytes = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        _write_file(zf, ENTRY_FILE, outputs[entry_uri])
        for resource_uri in sorted(records):
            path = paths[resource_uri]
            if path != ENTRY_FILE:
                _write_file(zf, path, outputs[resource_uri])
        _write_file(zf, MANIFEST_FILE, manifest_bytes)
    return out.getvalue()


def _write_file(zf: zipfile.ZipFile, path: str, data: bytes) -> None:
    info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    info.create_system = 3
    zf.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
