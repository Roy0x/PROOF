from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parents[2] / "proof.db"

def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS proof_packs (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, goal TEXT NOT NULL, first_verdict TEXT NOT NULL, final_verdict TEXT NOT NULL, repair_cycles INTEGER NOT NULL, payload TEXT NOT NULL)")

def save_pack(payload: dict[str, Any]) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute("INSERT INTO proof_packs(goal, first_verdict, final_verdict, repair_cycles, payload) VALUES (?, ?, ?, ?, ?)", (payload["goal"], payload["first_verification"]["verdict"], payload["final_verification"]["verdict"], payload.get("repair_cycles", 0), json.dumps(payload)))
        return int(cursor.lastrowid)

def list_packs(limit: int = 20) -> list[dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT id, created_at, goal, first_verdict, final_verdict, repair_cycles FROM proof_packs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

def get_pack(pack_id: int) -> dict[str, Any] | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT payload FROM proof_packs WHERE id = ?", (pack_id,)).fetchone()
        return json.loads(row[0]) if row else None
