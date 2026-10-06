"""Portable fixed-instant archive bundle."""

import io, json, zipfile, hashlib
from .storage import _parse_cutoff
from .validate import validate_target_uri


def export_bundle(archive, uri, cutoff):
    uri = validate_target_uri(uri)
    record = archive.lookup_at(uri, _parse_cutoff(cutoff))
    if record is None:
        raise ValueError("entry missing")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("index.html", record.body)
        z.writestr(
            "manifest.json",
            json.dumps(
                {
                    "entry": "index.html",
                    "cutoff": cutoff,
                    "records": [
                        {
                            "uri": uri,
                            "record_id": record.record_id,
                            "path": "index.html",
                            "sha256": hashlib.sha256(record.body).hexdigest(),
                        }
                    ],
                }
            ),
        )
    return out.getvalue()
