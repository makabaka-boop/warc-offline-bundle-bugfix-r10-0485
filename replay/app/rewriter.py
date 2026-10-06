"""Whitelist sanitizer + fixed-instant reference mapper.

The work is split into reusable pieces so the online replay service and the
offline bundle exporter share *one* implementation:

1. :func:`sanitize_html` keeps only an explicit whitelist of tags/attributes,
   so scripts, forms, styles, embeds and all event handlers are physically
   gone.  It returns a cleaned soup plus the document's effective base URI
   (the first legal ``<base>`` is used for resolution and then every
   ``<base>`` is removed).
2. :func:`map_references` rewrites the references of a sanitized page.
   ``lookup`` resolves a canonical URI at the fixed cutoff; ``map_url``
   decides the *literal* output for a resolved reference (an online
   ``/view|/raw`` URL for the service, a relative in-archive path for a
   bundle).  Anything missing / non-http(s) / wrong media type becomes an
   inert inline gap marker carrying no fetchable URL.

:func:`sanitize_and_rewrite` preserves the original online-replay entry
point used by the service.
"""

from __future__ import annotations

import html as _html
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

# ---- Explicit whitelists ---------------------------------------------------

ALLOWED_TAGS = frozenset(
    {
        "html",
        "head",
        "title",
        "body",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "p",
        "br",
        "hr",
        "blockquote",
        "pre",
        "div",
        "span",
        "ul",
        "ol",
        "li",
        "dl",
        "dt",
        "dd",
        "a",
        "img",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "th",
        "td",
        "caption",
        "colgroup",
        "col",
        "strong",
        "em",
        "b",
        "i",
        "u",
        "small",
        "sub",
        "sup",
        "code",
        "kbd",
        "samp",
        "var",
        "abbr",
        "cite",
        "q",
        "time",
        "figure",
        "figcaption",
    }
)

# Attributes are valid only for the tags they are listed for.
ALLOWED_ATTRS: dict[str, frozenset[str]] = {
    "a": frozenset({"href", "title"}),
    "img": frozenset({"src", "alt", "width", "height"}),
    "td": frozenset({"colspan", "rowspan"}),
    "th": frozenset({"colspan", "rowspan", "scope"}),
    "col": frozenset({"span"}),
    "colgroup": frozenset({"span"}),
    "time": frozenset({"datetime"}),
    "q": frozenset({"cite"}),
}

# Tags in this set disappear *with all their content*.  Everything else not
# whitelisted is unwrapped (text survives).
DROP_WITH_CONTENT = frozenset(
    {
        "script",
        "style",
        "iframe",
        "object",
        "embed",
        "applet",
        "noscript",
        "template",
        "form",
        "input",
        "button",
        "select",
        "textarea",
        "option",
        "optgroup",
        "output",
        "label",
        "fieldset",
        "legend",
        "datalist",
        "canvas",
        "svg",
        "math",
        "audio",
        "video",
        "track",
        "source",
        "frame",
        "frameset",
        "base",
        "link",
        "meta",
        "title-in-meta",
    }
)

HTML_CONTENT_TYPE = "text/html; charset=utf-8"
PNG_CONTENT_TYPE = "image/png"


@dataclass
class Ref:
    """One reference found on a sanitized page.

    Exactly one of ``record`` / ``gap`` is set: a same-cutoff capture of
    the expected media type produces ``record``; everything else produces
    a ``gap`` describing why the reference is inert.
    """

    kind: str  # "link" or "image"
    raw: str  # the raw attribute value as authored
    uri: str | None  # canonical resolved URI, or None for bad schemes
    record: object | None = None  # cutoff-selected record, if any
    gap: "Gap | None" = None  # why the reference could not be mapped

    @property
    def expect_type(self) -> str:
        return HTML_CONTENT_TYPE if self.kind == "link" else PNG_CONTENT_TYPE


@dataclass
class Gap:
    """One reference that could not be mapped to an in-archive resource."""

    kind: str
    raw: str
    reason: str  # stable machine-ish key: scheme|no-capture|wrong-type|empty
    detail: str  # human readable description
    has_capture: bool  # any capture at all exists (ignoring the cutoff)?
    capture_content_type: str | None = None


@dataclass
class SanitizedPage:
    soup: BeautifulSoup
    title: str | None
    base_href: str | None  # first legal <base>, or None
    document_base: str  # effective base used for resolution


@dataclass
class MapResult:
    html: str
    title: str | None
    base_href: str | None
    gaps: list[str]  # human-readable descriptions of every gap
    refs: list[Ref] = field(default_factory=list)
    gap_details: list[Gap] = field(default_factory=list)


# ---- step 1: structural sanitization ---------------------------------------


def sanitize_html(raw_html: bytes, archive_uri: str) -> SanitizedPage:
    """Decode, strip everything not whitelisted, resolve/remove ``<base>``."""
    text = raw_html.decode("utf-8")
    soup = BeautifulSoup(text, "html.parser")

    # Kill comments and processing instructions up front.
    for c in soup.find_all(
        string=lambda s: s.__class__.__name__
        in ("Comment", "CData", "ProcessingInstruction", "Doctype")
    ):
        c.extract()

    # Resolve the *first* legal <base href> then remove every <base>.
    base_href = None
    base_tag = soup.find("base")
    if base_tag is not None:
        candidate = base_tag.get("href", "").strip()
        if candidate:
            joined = urljoin(archive_uri, candidate)
            if _is_http_url(joined):
                base_href = joined
    for b in soup.find_all("base"):
        b.decompose()
    document_base = base_href or archive_uri

    # Structural sanitization: whitelist tags/attributes.
    for el in list(soup.find_all(True)):
        if el.parent is None:
            continue  # parent was already dropped with its content
        if el.name in ALLOWED_TAGS:
            allowed = ALLOWED_ATTRS.get(el.name, frozenset())
            for attr in list(el.attrs):
                if attr.lower() not in allowed:
                    del el.attrs[attr]
            continue
        if el.name in DROP_WITH_CONTENT:
            el.decompose()
        else:
            el.unwrap()

    title_tag = soup.find("title")
    title = title_tag.get_text() if title_tag else None
    if title is not None:
        title = title.strip() or None

    return SanitizedPage(soup, title, base_href, document_base)


def serialize(body_soup: BeautifulSoup, title: str | None) -> str:
    """Serialize a sanitized soup body into the inert document envelope."""
    if body_soup.body is not None:
        body_html = "".join(str(c) for c in body_soup.body.children)
    else:
        body_html = (
            "".join(str(c) for c in body_soup.find_all(recursive=False))
            or body_soup.decode()
        )
    head_extra = f"<title>{_html.escape(title)}</title>" if title else ""
    return (
        '<!DOCTYPE html>\n<html><head><meta charset="utf-8">'
        + head_extra
        + '</head><body data-replay="1">'
        + body_html
        + "</body></html>"
    )


# ---- step 2: fixed-cutoff reference mapping --------------------------------


def map_references(
    page: SanitizedPage,
    lookup,
    has_any_capture,
    map_url,
) -> MapResult:
    """Rewrite anchors/images in ``page`` against one fixed cutoff.

    ``lookup(canonical_uri) -> Record | None`` selects the record at the
    cutoff.  ``has_any_capture(canonical_uri) -> bool`` distinguishes
    "removed / only later" from "never archived".  ``map_url(ref, record)``
    returns the literal URL/relative path to write for a mapped reference.
    Each returned :class:`Ref` carries either its record or its :class:`Gap`.
    """
    refs: list[Ref] = []
    gaps: list[str] = []

    def fail(ref: Ref, gap: Gap, message: str) -> None:
        ref.gap = gap
        gaps.append(
            message if gap.reason == "empty" else f"{gap.kind} {ref.raw!r}: {message}"
        )

    # Images: only a same-cutoff PNG record may survive.
    for img in page.soup.find_all("img"):
        src = (img.get("src") or "").strip()
        ref = Ref("image", src, None)
        if src == "":
            why = "non-http(s) or unresolvable image source"
            fail(ref, Gap("img", src, "empty", why, False), why)
            _gap_for(page.soup, img, f"image gap: {why}")
        else:
            resolved = _resolve(page.document_base, src)
            ref.uri = resolved
            if resolved is None:
                why = "non-http(s) or unresolvable image source"
                fail(ref, Gap("img", src, "scheme", why, False), why)
                _gap_for(page.soup, img, f"image gap: {why}")
            else:
                target = lookup(resolved)
                ref.record = target
                if target is None:
                    gap = Gap(
                        "img",
                        src,
                        "no-capture",
                        "no archived capture at this instant",
                        has_any_capture(resolved),
                    )
                    fail(ref, gap, "no capture at this instant")
                    _gap_for(
                        page.soup,
                        img,
                        "image gap: no archived capture at this instant",
                    )
                elif target.content_type != PNG_CONTENT_TYPE:
                    gap = Gap(
                        "img",
                        src,
                        "wrong-type",
                        "archived resource is not PNG",
                        True,
                        target.content_type,
                    )
                    fail(ref, gap, "archived resource is not PNG")
                    _gap_for(page.soup, img, "image gap: archived resource is not PNG")
                else:
                    img["src"] = map_url(ref, target)
        refs.append(ref)

    # Links: only same-cutoff HTML records stay links.
    for a in page.soup.find_all("a"):
        href = (a.get("href") or "").strip()
        ref = Ref("link", href, None)
        if href == "":
            fail(
                ref,
                Gap("link", href, "empty", "anchor with no href: dropped", False),
                "anchor with no href: dropped",
            )
            a.unwrap()
        else:
            resolved = _resolve(page.document_base, href)
            ref.uri = resolved
            if resolved is None:
                fail(
                    ref,
                    Gap("link", href, "scheme", "non-http(s) scheme", False),
                    "non-http(s) scheme",
                )
                _text_gap_for(a, "link gap: scheme not archived")
            else:
                target = lookup(resolved)
                ref.record = target
                if target is None:
                    gap = Gap(
                        "link",
                        href,
                        "no-capture",
                        "no archived capture at this instant",
                        has_any_capture(resolved),
                    )
                    fail(ref, gap, "no capture at this instant")
                    _text_gap_for(a, "link gap: no archived capture at this instant")
                elif target.content_type != HTML_CONTENT_TYPE:
                    gap = Gap(
                        "link",
                        href,
                        "wrong-type",
                        "target is not archived HTML",
                        True,
                        target.content_type,
                    )
                    fail(ref, gap, "target is not archived HTML")
                    _text_gap_for(a, "link gap: target is not archived HTML")
                else:
                    a["href"] = map_url(ref, target)
        refs.append(ref)

    return MapResult(
        serialize(page.soup, page.title),
        page.title,
        page.base_href,
        gaps,
        refs,
        [r.gap for r in refs if r.gap is not None],
    )


# ---- online-replay convenience entry point ---------------------------------


@dataclass
class RewriteResult:
    html: str
    title: str | None
    base_href: str | None
    gaps: list[str]  # human-readable descriptions of every gap


def sanitize_and_rewrite(
    raw_html: bytes,
    archive_uri: str,
    cutoff: str,
    lookup,
    replay_url_for,
) -> RewriteResult:
    """Return inert HTML whose references point at this replay service.

    ``lookup(canonical_uri) -> Record | None`` resolves a mapped URI at the
    fixed cutoff; ``replay_url_for(uri, cutoff, raw=...)`` builds a
    replay-service URL.
    """
    page = sanitize_html(raw_html, archive_uri)
    result = map_references(
        page,
        lookup,
        lambda _u: False,  # service has no separate "any capture" index
        lambda ref, _rec: replay_url_for(
            ref.uri, cutoff, raw=(ref.kind == "image")
        ),
    )
    return RewriteResult(result.html, result.title, result.base_href, result.gaps)


# ---- helpers ---------------------------------------------------------------


def _is_http_url(url: str) -> bool:
    try:
        return urlsplit(url).scheme.lower() in ("http", "https") and bool(
            urlsplit(url).hostname
        )
    except ValueError:
        return False


def _resolve(base: str, ref: str) -> str | None:
    """Resolve ``ref`` against ``base``; return canonical URI or None."""
    from .validate import validate_target_uri

    try:
        joined = urljoin(base, ref)
    except (ValueError, TypeError):
        return None
    if not _is_http_url(joined):
        return None
    try:
        return validate_target_uri(joined)
    except Exception:
        # urljoin result could carry backslashes/control chars/userinfo.
        return None


def _gap_for(soup, img, label: str) -> None:
    """Replace an <img> with an inert placeholder span carrying no URL."""
    span = soup.new_tag("span")
    span["data-gap"] = "image"
    span.string = f"[{label}]"
    img.replace_with(span)


def _text_gap_for(a, label: str) -> None:
    """Turn an anchor into plain, bracketed gap text (no href remains)."""
    a.attrs = {}
    a.name = "span"
    a["data-gap"] = "link"
    text = a.get_text()
    a.string = f"[{text} — {label}]" if text else f"[{label}]"
