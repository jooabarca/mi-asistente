"""Local long-term memory stored in SQLite (memory.db), organized in folders (categories)."""

from __future__ import annotations

import re
import sqlite3
import threading
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import CATEGORY_BY_NAME, MEMORY_CATEGORIES

# Categories are validated in Python (config.CATEGORIES) rather than with a CHECK constraint,
# so adding a folder never needs a database migration.
SCHEMA = """
CREATE TABLE IF NOT EXISTS MemoryItem
(
      MemoryItemId INTEGER PRIMARY KEY AUTOINCREMENT
    , Category TEXT NOT NULL
    , Content TEXT NOT NULL
    , Profile TEXT NOT NULL
    , SourceType TEXT NOT NULL
    , CreatedAt TEXT NOT NULL
    , DueAt TEXT NULL
);

CREATE INDEX IF NOT EXISTS IX_MemoryItem_CreatedAt
    ON MemoryItem (CreatedAt);

CREATE INDEX IF NOT EXISTS IX_MemoryItem_Category
    ON MemoryItem (Category, CreatedAt);

CREATE TABLE IF NOT EXISTS SessionLog
(
      SessionLogId INTEGER PRIMARY KEY AUTOINCREMENT
    , Profile TEXT NOT NULL
    , Model TEXT NOT NULL
    , StartedAt TEXT NOT NULL
    , EndedAt TEXT NOT NULL
    , ResponseCount INTEGER NOT NULL
    , InputTokens INTEGER NOT NULL
    , OutputTokens INTEGER NOT NULL
    , EstimatedCostUsd REAL
);
"""

_COLUMNS = """
      MemoryItemId
    , Category
    , Content
    , Profile
    , SourceType
    , CreatedAt
    , DueAt
"""

# Words ignored when searching (English + Spanish).
_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "about", "what", "did", "i", "my", "me",
    "is", "was", "that", "this", "it", "with", "save", "saved", "remember", "anything", "do", "you", "know",
    "el", "la", "los", "las", "un", "una", "de", "del", "que", "en", "y", "o", "sobre", "mi", "mis", "lo",
    "guarde", "guardado", "recuerdas", "sabes", "tengo", "hay", "algo", "cosas",
}

SOURCE_USER_REQUEST = "UserRequest"
SOURCE_AUTO = "Auto"
SOURCE_MANUAL = "Manual"  # typed in the app window


@dataclass
class MemoryItem:
    memory_item_id: int
    category: str
    content: str
    profile: str
    source_type: str
    created_at: str
    due_at: str | None = None

    def as_dict(self) -> dict:
        data = {
            "MemoryItemId": self.memory_item_id,
            "Category": self.category,
            "Content": self.content,
            "Profile": self.profile,
            "SourceType": self.source_type,
            "CreatedAt": self.created_at,
        }
        if self.due_at:
            data["DueAt"] = self.due_at
        return data


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _fold(text: str) -> str:
    """Lowercase without accents, so 'canción' matches 'cancion'."""
    return "".join(c for c in unicodedata.normalize("NFD", text.lower()) if unicodedata.category(c) != "Mn")


def _stems(text: str) -> set[str]:
    """Crude singular forms, so 'canciones' matches 'canción' and 'ideas' matches 'idea'."""
    stems = set()
    for word in re.findall(r"\w+", _fold(text)):
        if word.endswith("ones"):
            word = word[:-2]
        elif word.endswith("s") and len(word) > 3:
            word = word[:-1]
        stems.add(word)
    return stems


def normalize_due_at(value) -> str | None:
    """Keep 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM' (seconds/timezone are dropped); anything else -> None."""
    if not value:
        return None
    text = str(value).strip().replace(" ", "T")
    for length, fmt in ((16, "%Y-%m-%dT%H:%M"), (10, "%Y-%m-%d")):
        try:
            return datetime.strptime(text[:length], fmt).strftime(fmt)
        except ValueError:
            continue
    return None


class MemoryStore:
    """Thread-safe: the conversation loop writes while the app window reads."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self.version = 0  # bumps on every change, so the window knows when to refresh
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self._migrate()
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def _migrate(self) -> None:
        row = self.conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'MemoryItem'").fetchone()
        if row is None:
            return
        if "CHECK" in row[0]:  # first version limited Category with a CHECK constraint: rebuild without it
            self.conn.executescript(
                """
                ALTER TABLE MemoryItem RENAME TO MemoryItemOld;
                DROP INDEX IF EXISTS IX_MemoryItem_CreatedAt;
                """
            )
            self.conn.executescript(SCHEMA)
            self.conn.execute(
                """
                INSERT INTO MemoryItem
                (
                      MemoryItemId
                    , Category
                    , Content
                    , Profile
                    , SourceType
                    , CreatedAt
                )
                SELECT
                      MemoryItemId
                    , CASE WHEN Category = 'Reminder' THEN 'Agenda' ELSE Category END
                    , Content
                    , Profile
                    , SourceType
                    , CreatedAt
                FROM MemoryItemOld
                """
            )
            self.conn.execute("DROP TABLE MemoryItemOld")
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def _rows(self, sql: str, params=()) -> list[MemoryItem]:
        with self._lock:
            return [MemoryItem(*row) for row in self.conn.execute(sql, params).fetchall()]

    # ------------------------------------------------------------------ MemoryItem

    def save(self, category: str, content: str, profile: str, source_type: str, due_at=None) -> MemoryItem:
        if category not in CATEGORY_BY_NAME:
            raise ValueError(f"Unknown category '{category}'. Valid: {', '.join(MEMORY_CATEGORIES)}")
        content = content.strip()
        if not content:
            raise ValueError("Content is empty.")
        due_at = normalize_due_at(due_at)
        created_at = _now()
        with self._lock:
            cursor = self.conn.execute(
                """
                INSERT INTO MemoryItem
                (
                      Category
                    , Content
                    , Profile
                    , SourceType
                    , CreatedAt
                    , DueAt
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (category, content, profile, source_type, created_at, due_at),
            )
            self.conn.commit()
            self.version += 1
        return MemoryItem(cursor.lastrowid, category, content, profile, source_type, created_at, due_at)

    def delete(self, memory_item_id: int) -> bool:
        with self._lock:
            cursor = self.conn.execute(
                """
                DELETE FROM MemoryItem
                WHERE MemoryItemId = ?
                """,
                (memory_item_id,),
            )
            self.conn.commit()
            self.version += 1
        return cursor.rowcount > 0

    def list_category(self, category: str | None = None, text: str = "") -> list[MemoryItem]:
        """Items of one folder (or all), optionally filtered by text. Agenda is sorted by date."""
        where, params = [], []
        if category:
            where.append("Category = ?")
            params.append(category)
        items = self._rows(
            f"""
            SELECT
                {_COLUMNS}
            FROM MemoryItem
            {"WHERE " + " AND ".join(where) if where else ""}
            ORDER BY
                  CASE WHEN DueAt IS NULL THEN 1 ELSE 0 END
                , DueAt
                , CreatedAt DESC
            """ if category == "Agenda" else
            f"""
            SELECT
                {_COLUMNS}
            FROM MemoryItem
            {"WHERE " + " AND ".join(where) if where else ""}
            ORDER BY
                  CreatedAt DESC
                , MemoryItemId DESC
            """,
            params,
        )
        if text.strip():
            needle = _fold(text.strip())
            items = [i for i in items if needle in _fold(i.content)]
        return items

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT
                      Category
                    , COUNT(*) AS ItemCount
                FROM MemoryItem
                GROUP BY Category
                """
            ).fetchall()
        return dict(rows)

    def search(self, query: str, limit: int = 8) -> list[MemoryItem]:
        """Keyword search: items matching more words rank higher, then newer first.
        Naming a folder ('canciones', 'agenda', 'recommendations') searches inside it."""
        query_stems = _stems(query)
        categories = [c.name for c in CATEGORY_BY_NAME.values()
                      if _stems(c.label) <= query_stems or c.name.lower() in query_stems]
        folder_stems = set().union(*(_stems(CATEGORY_BY_NAME[c].label) for c in categories))
        words = [w for w in re.findall(r"\w+", _fold(query))
                 if len(w) > 1 and w not in _STOPWORDS and not (_stems(w) & folder_stems)]
        if not words and not categories:
            return self.recent(limit)

        score_terms = ["CASE WHEN lower(Content) LIKE ? THEN 1 ELSE 0 END" for _ in words]
        score_terms += ["CASE WHEN Category = ? THEN 1 ELSE 0 END" for _ in categories]
        params: list = [f"%{w}%" for w in words] + categories
        score_sql = "\n                    + ".join(score_terms)
        return self._rows(
            f"""
            SELECT
                {_COLUMNS}
            FROM
            (
                SELECT
                    {_COLUMNS}
                    , ({score_sql}) AS Score
                FROM MemoryItem
            )
            WHERE Score > 0
            ORDER BY
                  Score DESC
                , CreatedAt DESC
            LIMIT ?
            """,
            (*params, limit),
        )

    def recent(self, limit: int = 10, profile: str | None = None) -> list[MemoryItem]:
        """Most recent items; items from the given profile come first."""
        return self._rows(
            f"""
            SELECT
                {_COLUMNS}
            FROM MemoryItem
            ORDER BY
                  CASE WHEN Profile = ? THEN 0 ELSE 1 END
                , CreatedAt DESC
                , MemoryItemId DESC
            LIMIT ?
            """,
            (profile or "", limit),
        )

    def summary(self, profile: str, limit: int) -> str:
        """What the model knows at the start of a session ('' if nothing is saved):
        everything About the user, upcoming Agenda items, then the most recent of the rest."""
        about = self._rows(
            f"""
            SELECT
                {_COLUMNS}
            FROM MemoryItem
            WHERE Category = 'AboutMe'
            ORDER BY CreatedAt DESC
            LIMIT 30
            """
        )
        today = datetime.now().strftime("%Y-%m-%d")
        upcoming = self._rows(
            f"""
            SELECT
                {_COLUMNS}
            FROM MemoryItem
            WHERE Category = 'Agenda'
                AND (DueAt IS NULL OR DueAt >= ?)
            ORDER BY
                  CASE WHEN DueAt IS NULL THEN 1 ELSE 0 END
                , DueAt
            LIMIT 8
            """,
            (today,),
        )
        shown = {i.memory_item_id for i in about + upcoming}
        others = [i for i in self.recent(limit + len(shown), profile)
                  if i.memory_item_id not in shown and i.category not in ("AboutMe", "Agenda")][:max(0, limit)]

        def line(item: MemoryItem) -> str:
            content = item.content if len(item.content) <= 200 else item.content[:197] + "..."
            when = f"due {item.due_at}" if item.due_at else f"saved {item.created_at[:10]}"
            return f"- [{item.category}, {when}] {content}"

        sections = []
        if about:
            sections.append("About the user:\n" + "\n".join(f"- {i.content}" for i in about))
        if upcoming:
            sections.append("Agenda (upcoming):\n" + "\n".join(line(i) for i in upcoming))
        if others:
            sections.append("Recently saved:\n" + "\n".join(line(i) for i in others))
        return "\n".join(sections)

    def count(self) -> int:
        with self._lock:
            return self.conn.execute("SELECT COUNT(*) FROM MemoryItem").fetchone()[0]

    # ------------------------------------------------------------------ SessionLog

    def log_session(
        self,
        profile: str,
        model: str,
        started_at: str,
        response_count: int,
        input_tokens: int,
        output_tokens: int,
        estimated_cost_usd: float | None,
    ) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO SessionLog
                (
                      Profile
                    , Model
                    , StartedAt
                    , EndedAt
                    , ResponseCount
                    , InputTokens
                    , OutputTokens
                    , EstimatedCostUsd
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (profile, model, started_at, _now(), response_count, input_tokens, output_tokens, estimated_cost_usd),
            )
            self.conn.commit()
