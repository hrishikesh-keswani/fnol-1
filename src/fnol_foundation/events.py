"""One writer for the append-only flight recorder.

Callers supply the columns. This helper only requires an event name and a span id
so a step cannot log a row that forgets what happened.
"""

from __future__ import annotations

import uuid
from typing import Any

from fnol_foundation.stores import Stores


def record_event(stores: Stores, **fields: Any) -> str:
    if not fields.get("event"):
        raise ValueError("event name is required")
    fields["span_id"] = fields.get("span_id") or uuid.uuid4().hex
    stores.trace.append_event(**fields)
    return fields["span_id"]
