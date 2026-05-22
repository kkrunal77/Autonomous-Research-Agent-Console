"""
memory.py — Persistent Memory Layer.

Stores facts that survive across runs in state/memory.json.
Stores per-session history in state/history.json.

Functions:
    save_memory(key, value, tags)  → MemoryRecord
    load_memory()                  → List[MemoryRecord]
    search_memory(query)           → List[MemoryRecord]
    append_history(entry)          → None
    load_history()                 → List[HistoryEntry]
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from schemas import HistoryEntry, MemoryRecord

# ── paths ────────────────────────────────────────
ROOT = Path(__file__).parent
STATE_DIR = ROOT / "state"
MEMORY_PATH = STATE_DIR / "memory.json"
HISTORY_PATH = STATE_DIR / "history.json"


def _ensure_state_dir() -> None:
    STATE_DIR.mkdir(exist_ok=True)
    if not MEMORY_PATH.exists():
        MEMORY_PATH.write_text("[]", encoding="utf-8")
    if not HISTORY_PATH.exists():
        HISTORY_PATH.write_text("[]", encoding="utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────
# Memory CRUD
# ─────────────────────────────────────────────────

def load_memory() -> List[MemoryRecord]:
    """Load all persisted memory records."""
    _ensure_state_dir()
    try:
        data = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
        return [MemoryRecord.model_validate(r) for r in data]
    except (json.JSONDecodeError, Exception):
        return []


def save_memory(key: str, value: str, tags: List[str] | None = None) -> MemoryRecord:
    """Save or update a fact in persistent memory."""
    _ensure_state_dir()
    records = load_memory()

    # Update existing key if found
    for rec in records:
        if rec.key.lower() == key.lower():
            rec.value = value
            rec.timestamp = _now()
            rec.tags = tags or rec.tags
            _write_memory(records)
            return rec

    # Create new record
    new_rec = MemoryRecord(key=key, value=value, timestamp=_now(), tags=tags or [])
    records.append(new_rec)
    _write_memory(records)
    return new_rec


def _write_memory(records: List[MemoryRecord]) -> None:
    MEMORY_PATH.write_text(
        json.dumps([r.model_dump() for r in records], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def search_memory(query: str) -> List[MemoryRecord]:
    """Fuzzy keyword search over keys and values."""
    records = load_memory()
    if not query:
        return records
    q = query.lower()
    results = []
    for rec in records:
        if q in rec.key.lower() or q in rec.value.lower() or any(q in t.lower() for t in rec.tags):
            results.append(rec)
    return results


def delete_memory(key: str) -> bool:
    """Remove a memory record by key. Returns True if found and deleted."""
    records = load_memory()
    new = [r for r in records if r.key.lower() != key.lower()]
    if len(new) == len(records):
        return False
    _write_memory(new)
    return True


# ─────────────────────────────────────────────────
# History
# ─────────────────────────────────────────────────

def load_history() -> List[HistoryEntry]:
    _ensure_state_dir()
    try:
        data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        return [HistoryEntry.model_validate(e) for e in data]
    except Exception:
        return []


def append_history(entry: HistoryEntry) -> None:
    _ensure_state_dir()
    history = load_history()
    history.append(entry)
    # Keep last 200 entries to avoid unbounded growth
    history = history[-200:]
    HISTORY_PATH.write_text(
        json.dumps([e.model_dump() for e in history], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def format_memory_for_prompt(records: List[MemoryRecord]) -> str:
    """Format memory records as a bullet list for injection into prompts."""
    if not records:
        return "(no relevant memory found)"
    lines = [f"- {r.key}: {r.value}" for r in records]
    return "\n".join(lines)
