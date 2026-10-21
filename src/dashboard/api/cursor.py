"""Opaque keyset cursor for the dashboard's paged journal (design §3.4).

The journal pages on ``(signals.created_at DESC, signals.id DESC)``. The cursor the
client echoes back is ``base64url(json.dumps([created_at_iso, str(id)]))`` with the
``=`` padding stripped so it is URL-safe without percent-encoding. It carries no
secret -- it is just a bookmark -- but it *is* untrusted input, so ``decode_cursor``
turns every malformed shape into :class:`BadCursor` (a ``ValueError``) for the
router to map to HTTP 400 ``bad_cursor`` rather than a 500.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from uuid import UUID

__all__ = ["BadCursor", "decode_cursor", "encode_cursor"]


class BadCursor(ValueError):  # noqa: N818 - name fixed by the Step 4.2 plan / router contract
    """The cursor string is not one this module produced (tampered / truncated)."""


def encode_cursor(created_at: datetime, id: UUID) -> str:
    """Encode a ``(created_at, id)`` keyset position as an opaque URL-safe token."""
    raw = json.dumps([created_at.isoformat(), str(id)]).encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(raw: str) -> tuple[datetime, UUID]:
    """Inverse of :func:`encode_cursor`; padded input is accepted too.

    Raises:
        BadCursor: on bad base64, bad JSON, the wrong element count / types, a
            non-ISO or naive timestamp, or a malformed UUID.
    """
    try:
        padded = raw + "=" * (-len(raw) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        parsed = json.loads(decoded)
    except ValueError as exc:  # binascii.Error, JSONDecodeError, Unicode*Error
        raise BadCursor("cursor is not base64url-encoded JSON") from exc

    if not isinstance(parsed, list) or len(parsed) != 2:
        raise BadCursor("cursor must be a two-element list")
    ts_raw, id_raw = parsed
    if not isinstance(ts_raw, str) or not isinstance(id_raw, str):
        raise BadCursor("cursor elements must be strings")
    try:
        created_at = datetime.fromisoformat(ts_raw)
        signal_id = UUID(id_raw)
    except ValueError as exc:
        raise BadCursor("cursor timestamp or id is malformed") from exc
    if created_at.tzinfo is None:
        raise BadCursor("cursor timestamp must be timezone-aware")
    return created_at, signal_id
