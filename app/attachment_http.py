"""Serve private SQLite attachments, with single byte ranges for media playback."""
import re
import sqlite3

from fastapi import HTTPException
from fastapi.responses import Response


_MEDIA_TYPES = frozenset({
    "audio/mpeg", "audio/mp4", "audio/ogg", "audio/wav", "audio/webm", "audio/x-wav",
    "image/jpeg", "image/png", "image/webp",
})
_BYTE_RANGE = re.compile(r"([0-9]*)-([0-9]*)")


def _bounded_number(digits: str, limit: int) -> int:
    # Avoid Python's decimal conversion limit for client-controlled header values.
    digits = digits.lstrip("0")
    if len(digits) > len(str(limit)):
        return limit
    return min(int(digits or "0"), limit)


def _byte_bounds(value: str, size: int) -> tuple[int, int] | None:
    """Return inclusive bounds; ignore unsupported units/multiple ranges."""
    unit, equals, bounds = value.strip().partition("=")
    if not equals or unit.lower() != "bytes" or "," in bounds:
        return None
    match = _BYTE_RANGE.fullmatch(bounds)
    if not match or not size or not any(match.groups()):
        raise ValueError("invalid or unsatisfiable byte range")
    first, last = match.groups()
    if not first:
        suffix = _bounded_number(last, size)
        if not suffix:
            raise ValueError("zero-length suffix")
        return size - suffix, size - 1
    start = _bounded_number(first, size)
    end = min(_bounded_number(last, size), size - 1) if last else size - 1
    if start > end:
        raise ValueError("unsatisfiable byte range")
    return start, end


def serve_attachment(
    con: sqlite3.Connection, attachment_id: int, range_header: str | None = None, head: bool = False
) -> Response:
    missing = HTTPException(404, "该附件还不可用。")
    if not -(2**63) <= attachment_id < 2**63:
        raise missing
    asset = con.execute(
        "SELECT mime,length(body) FROM attachments WHERE id=? AND state='ready' AND body IS NOT NULL",
        (attachment_id,),
    ).fetchone()
    if asset is None:
        raise missing
    mime, size = asset
    media_type = mime if mime in _MEDIA_TYPES else "application/octet-stream"
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    # Range is defined for GET; HEAD reports the full representation's headers.
    try:
        bounds = _byte_bounds(range_header, size) if range_header and not head else None
    except ValueError:
        return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
    if head:
        return Response(media_type=media_type, headers={**headers, "Content-Length": str(size)})

    if bounds is None:
        body = con.execute(
            "SELECT body FROM attachments WHERE id=? AND state='ready'", (attachment_id,)
        ).fetchone()
    else:
        start, end = bounds
        body = con.execute(
            "SELECT substr(body,?,?) FROM attachments WHERE id=? AND state='ready'",
            (start + 1, end - start + 1, attachment_id),
        ).fetchone()
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    if body is None or body[0] is None:
        raise missing
    return Response(body[0], status_code=206 if bounds is not None else 200, media_type=media_type, headers=headers)
