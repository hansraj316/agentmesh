"""Import AMP events from JSON Lines files (the TypeScript SDK bridge)."""

import json
from pathlib import Path
from typing import List, Tuple, Union

from agentmesh.events import Event
from agentmesh.store import EventStore


def import_jsonl(path: Union[str, Path], store: EventStore) -> Tuple[int, int]:
    """Import a JSONL file of AMP events into ``store``.

    The whole file is parsed and validated before anything is written, so a
    malformed file imports nothing. Duplicate event_ids (e.g. re-importing
    the same file) are skipped via the store's ignore-duplicates path.

    Returns ``(imported, skipped)``. Raises ``FileNotFoundError`` if the
    file is missing and ``ValueError`` naming the offending line number for
    malformed lines.
    """
    text = Path(path).read_text(encoding="utf-8")
    events: List[Event] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("line %d: not valid JSON: %s" % (number, exc))
        if not isinstance(data, dict):
            raise ValueError(
                "line %d: expected a JSON object, got %s" % (number, type(data).__name__)
            )
        try:
            events.append(Event.from_dict(data))
        except KeyError as exc:
            raise ValueError("line %d: missing field %s" % (number, exc))
        except ValueError as exc:
            raise ValueError("line %d: invalid AMP event: %s" % (number, exc))
    imported = 0
    skipped = 0
    for event in events:
        if store.append_ignore_duplicates(event):
            imported += 1
        else:
            skipped += 1
    return imported, skipped
