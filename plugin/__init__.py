"""shmulsidian memory plugin — MemoryProvider backed by a local Obsidian vault.

Provides cross-session memory via FTS5 + semantic search over markdown notes
in a shmulsidian-structured vault. The vault is a git repo with PARA-style
folders (00_Inbox, 01_Zettelkasten, 02_Projects, etc.) and a MEMORY.md index.

Features:
- Hybrid search (FTS5 keyword + sqlite-vec semantic) over vault notes
- Auto-saves session summaries as notes in 00_Inbox
- Injects MEMORY.md index into system prompt for quick navigation
- Tools for searching, reading, and creating vault notes
- Mirrors built-in memory writes to vault notes

Config (env vars):
  SHMULSIDIAN_VAULT_PATH  — path to the vault (default: ~/shmulsidian)

Requires: sqlite-vec, fastembed (for semantic search)
Falls back to FTS5-only if sqlite-vec/fastembed are unavailable.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import sqlite3
import struct
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHUNK_SIZE = 800
CHUNK_OVERLAP = 120
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384
MD_SUFFIX = ".md"
SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._\- ]+")

# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

SEARCH_NOTES_SCHEMA = {
    "name": "shmulsidian_search",
    "description": (
        "Search the shmulsidian vault for notes matching a query. "
        "Uses hybrid search (70% semantic + 30% keyword) when available, "
        "falls back to FTS5 keyword search. Returns note paths, matching "
        "chunks, and relevance scores."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to search for in the vault.",
            },
            "k": {
                "type": "integer",
                "description": "Max results (default: 10).",
            },
        },
        "required": ["query"],
    },
}

READ_NOTE_SCHEMA = {
    "name": "shmulsidian_read",
    "description": (
        "Read a single note from the vault by its vault-relative path. "
        "Returns the full markdown content with frontmatter."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Vault-relative path, e.g. '00_Inbox/my-note.md'",
            },
        },
        "required": ["path"],
    },
}

CREATE_NOTE_SCHEMA = {
    "name": "shmulsidian_create",
    "description": (
        "Create a new note in the vault. Adds YAML frontmatter with title, "
        "created date, and optional tags. Defaults to 00_Inbox folder."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Note title.",
            },
            "body": {
                "type": "string",
                "description": "Note content (markdown).",
            },
            "folder": {
                "type": "string",
                "description": "Target folder (default: 00_Inbox).",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tags for the note.",
            },
        },
        "required": ["title", "body"],
    },
}

LIST_NOTES_SCHEMA = {
    "name": "shmulsidian_list",
    "description": (
        "List notes in the vault, optionally filtered to a folder. "
        "Returns paths, sizes, and modification times. Newest first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "folder": {
                "type": "string",
                "description": "Optional folder prefix to filter by.",
            },
            "limit": {
                "type": "integer",
                "description": "Max results (default: 20).",
            },
        },
        "required": [],
    },
}

ALL_SCHEMAS = [SEARCH_NOTES_SCHEMA, READ_NOTE_SCHEMA, CREATE_NOTE_SCHEMA, LIST_NOTES_SCHEMA]


# ---------------------------------------------------------------------------
# Vault filesystem helpers
# ---------------------------------------------------------------------------

def _safe_join(vault: Path, rel: str) -> Path:
    """Join paths and reject anything that escapes the vault."""
    candidate = (vault / rel).resolve()
    try:
        candidate.relative_to(vault.resolve())
    except ValueError:
        raise PermissionError(f"path escapes vault: {rel}")
    return candidate


def _iter_md_files(vault: Path) -> list[tuple[str, float, int]]:
    """Yield (posix_path, mtime, size) for all .md files in the vault."""
    out = []
    for p in vault.rglob(f"*{MD_SUFFIX}"):
        if not p.is_file():
            continue
        # skip dotted dirs (.git, .obsidian, .shmulsidian, etc.)
        if any(part.startswith(".") for part in p.relative_to(vault).parts):
            continue
        st = p.stat()
        out.append((p.relative_to(vault).as_posix(), st.st_mtime, st.st_size))
    return out


def _read_note_file(vault: Path, path: str) -> dict:
    p = _safe_join(vault, path)
    if not p.is_file():
        raise FileNotFoundError(f"note not found: {path}")
    content = p.read_text(encoding="utf-8", errors="replace")
    st = p.stat()
    return {"path": path, "content": content, "size": st.st_size, "mtime": st.st_mtime}


def _create_note_file(vault: Path, title: str, body: str,
                      folder: str = "00_Inbox", tags: list[str] | None = None) -> dict:
    folder_path = _safe_join(vault, folder)
    folder_path.mkdir(parents=True, exist_ok=True)

    stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
    slug = SAFE_FILENAME.sub("", title).strip().replace(" ", "-").lower()[:60] or "note"
    fname = f"{stamp}-{slug}{MD_SUFFIX}"
    target = folder_path / fname
    if target.exists():
        import os as _os
        target = folder_path / f"{stamp}-{slug}-{_os.urandom(2).hex()}{MD_SUFFIX}"

    fm_lines = [
        "---",
        f"title: {title}",
        f"created: {dt.datetime.now().isoformat(timespec='seconds')}",
    ]
    if tags:
        fm_lines.append("tags: [" + ", ".join(tags) + "]")
    fm_lines.append("---")
    target.write_text("\n".join(fm_lines) + "\n\n" + body.rstrip() + "\n", encoding="utf-8")

    rel = target.relative_to(vault).as_posix()
    return {"path": rel, "size": target.stat().st_size}


def _list_notes(vault: Path, folder: str | None = None, limit: int = 20) -> list[dict]:
    refs = _iter_md_files(vault)
    if folder:
        prefix = folder.rstrip("/") + "/"
        refs = [(p, m, s) for p, m, s in refs if p.startswith(prefix)]
    refs.sort(key=lambda r: r[1], reverse=True)
    return [{"path": p, "mtime": m, "size": s} for p, m, s in refs[:limit]]


# ---------------------------------------------------------------------------
# Vault index (SQLite FTS5 + sqlite-vec)
# ---------------------------------------------------------------------------

class VaultIndex:
    """Lightweight SQLite index over the vault for search."""

    def __init__(self, vault: Path):
        self.vault = vault
        self.db_path = vault / ".shmulsidian" / "index.sqlite"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._embedder = None
        self._has_vec = False
        self._conn: Optional[sqlite3.Connection] = None
        self._init_lock = threading.Lock()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            with self._init_lock:
                if self._conn is None:
                    self._conn = sqlite3.connect(str(self.db_path), timeout=30.0)
                    self._conn.execute("PRAGMA journal_mode=WAL")
                    self._conn.execute("PRAGMA busy_timeout=30000")
                    self._migrate()
        return self._conn

    def _migrate(self) -> None:
        c = self._conn
        c.executescript(f"""
            CREATE TABLE IF NOT EXISTS notes (
                path  TEXT PRIMARY KEY,
                mtime REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chunks (
                id    INTEGER PRIMARY KEY AUTOINCREMENT,
                path  TEXT NOT NULL,
                idx   INTEGER NOT NULL,
                text  TEXT NOT NULL,
                FOREIGN KEY (path) REFERENCES notes(path) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS chunks_path_idx ON chunks(path);
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
                USING fts5(text, content='chunks', content_rowid='id');
        """)
        # Try to create vec table (needs sqlite-vec extension)
        try:
            c.enable_load_extension(True)
            import sqlite_vec
            sqlite_vec.load(c)
            c.enable_load_extension(False)
            c.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec
                    USING vec0(embedding float[{EMBED_DIM}]);
            """)
            self._has_vec = True
        except Exception:
            c.enable_load_extension(False)
            self._has_vec = False
            logger.debug("sqlite-vec not available; using FTS5-only search")
        c.commit()

    def _embed(self, texts: list[str]) -> list[list[float]]:
        if self._embedder is None:
            from fastembed import TextEmbedding
            self._embedder = TextEmbedding(model_name=EMBED_MODEL)
        return [list(v) for v in self._embedder.embed(texts)]

    @staticmethod
    def _pack(vec: list[float]) -> bytes:
        return struct.pack(f"{len(vec)}f", *vec)

    def reindex(self) -> dict:
        """Re-index changed notes. Returns counters."""
        c = self._get_conn()
        refs = _iter_md_files(self.vault)
        current = {p: m for p, m, _ in refs}
        existing = dict(c.execute("SELECT path, mtime FROM notes").fetchall())

        to_remove = [p for p in existing if p not in current]
        to_upsert = [p for p, m in current.items() if existing.get(p) != m]

        for path in to_remove:
            self._delete_note(path)

        added = 0
        for path in to_upsert:
            self._delete_note(path)
            self._index_note(path)
            added += 1
        c.commit()
        return {"indexed": added, "removed": len(to_remove), "total": len(current)}

    def _delete_note(self, path: str) -> None:
        c = self._get_conn()
        rows = c.execute("SELECT id FROM chunks WHERE path = ?", (path,)).fetchall()
        ids = [r[0] for r in rows]
        if ids:
            qmarks = ",".join("?" * len(ids))
            c.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({qmarks})", ids)
            if self._has_vec:
                try:
                    c.execute(f"DELETE FROM chunks_vec WHERE rowid IN ({qmarks})", ids)
                except Exception:
                    pass
            c.execute(f"DELETE FROM chunks WHERE id IN ({qmarks})", ids)
        c.execute("DELETE FROM notes WHERE path = ?", (path,))

    def _index_note(self, path: str) -> None:
        c = self._get_conn()
        try:
            full = (self.vault / path).read_text(encoding="utf-8", errors="replace")
        except Exception:
            return
        chunks = _chunk(full)
        if not chunks:
            return

        mtime = (self.vault / path).stat().st_mtime
        c.execute("INSERT INTO notes(path, mtime) VALUES (?, ?)", (path, mtime))

        vectors = None
        if self._has_vec:
            try:
                vectors = self._embed(chunks)
            except Exception:
                vectors = None

        for idx, text in enumerate(chunks):
            cur = c.execute(
                "INSERT INTO chunks(path, idx, text) VALUES (?, ?, ?)",
                (path, idx, text),
            )
            rowid = cur.lastrowid
            c.execute("INSERT INTO chunks_fts(rowid, text) VALUES (?, ?)", (rowid, text))
            if vectors and idx < len(vectors):
                try:
                    c.execute(
                        "INSERT INTO chunks_vec(rowid, embedding) VALUES (?, ?)",
                        (rowid, self._pack(vectors[idx])),
                    )
                except Exception:
                    pass

    def search_keyword(self, query: str, k: int = 10) -> list[dict]:
        c = self._get_conn()
        rows = c.execute("""
            SELECT c.path, c.text, bm25(chunks_fts) AS score
              FROM chunks_fts
              JOIN chunks c ON c.id = chunks_fts.rowid
             WHERE chunks_fts MATCH ?
             ORDER BY score
             LIMIT ?
        """, (query, k)).fetchall()
        return [{"path": p, "chunk": t, "score": -s} for p, t, s in rows]

    def search_semantic(self, query: str, k: int = 10) -> list[dict]:
        if not self._has_vec:
            return []
        c = self._get_conn()
        qvec = self._embed([query])[0]
        rows = c.execute("""
            SELECT c.path, c.text, v.distance
              FROM chunks_vec v
              JOIN chunks c ON c.id = v.rowid
             WHERE v.embedding MATCH ?
               AND k = ?
             ORDER BY v.distance
        """, (self._pack(qvec), k)).fetchall()
        return [{"path": p, "chunk": t, "score": max(0.0, 1.0 - d / 2.0)} for p, t, d in rows]

    def search_hybrid(self, query: str, k: int = 10, sem_weight: float = 0.7) -> list[dict]:
        if not self._has_vec:
            return self.search_keyword(query, k)
        kw_weight = 1.0 - sem_weight
        sem = {(h["path"], h["chunk"]): h["score"] for h in self.search_semantic(query, k * 2)}
        kw = {(h["path"], h["chunk"]): h["score"] for h in self.search_keyword(query, k * 2)}

        def norm(d: dict) -> dict:
            if not d:
                return {}
            top = max(d.values()) or 1.0
            return {k: v / top for k, v in d.items()}

        sem_n, kw_n = norm(sem), norm(kw)
        keys = set(sem_n) | set(kw_n)
        scored = [
            {"path": p, "chunk": t,
             "score": sem_weight * sem_n.get((p, t), 0.0) + kw_weight * kw_n.get((p, t), 0.0)}
            for (p, t) in keys
        ]
        scored.sort(key=lambda h: h["score"], reverse=True)
        return scored[:k]


def _chunk(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    out, i = [], 0
    while i < len(text):
        out.append(text[i:i + CHUNK_SIZE])
        i += CHUNK_SIZE - CHUNK_OVERLAP
    return out


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------

class ShmulsidianMemoryProvider(MemoryProvider):
    """Obsidian vault memory — FTS5 + semantic search, session notes, MEMORY.md injection."""

    def __init__(self):
        self._vault: Optional[Path] = None
        self._index: Optional[VaultIndex] = None
        self._session_id = ""
        self._prefetch_result = ""
        self._prefetch_lock = threading.Lock()
        self._prefetch_thread: Optional[threading.Thread] = None
        self._turn_count = 0
        self._initialized = False

    @property
    def name(self) -> str:
        return "shmulsidian"

    def is_available(self) -> bool:
        """Check if the vault path exists and contains notes."""
        vault_path = self._resolve_vault_path()
        return vault_path.is_dir() and any(vault_path.rglob(f"*{MD_SUFFIX}"))

    def _resolve_vault_path(self) -> Path:
        env_path = os.environ.get("SHMULSIDIAN_VAULT_PATH", "")
        if env_path:
            return Path(env_path).expanduser().resolve()
        return Path.home() / "shmulsidian"

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "vault_path",
                "description": "Path to the shmulsidian vault",
                "default": "~/shmulsidian",
                "env_var": "SHMULSIDIAN_VAULT_PATH",
            },
        ]

    def initialize(self, session_id: str, **kwargs) -> None:
        """Initialize vault connection and start background index."""
        try:
            self._vault = self._resolve_vault_path()
            if not self._vault.is_dir():
                logger.warning("shmulsidian vault not found at %s", self._vault)
                return

            self._session_id = session_id
            self._index = VaultIndex(self._vault)
            self._initialized = True

            # Background re-index on startup
            def _reindex():
                try:
                    result = self._index.reindex()
                    logger.debug("shmulsidian index: %s", result)
                except Exception as e:
                    logger.debug("shmulsidian reindex failed: %s", e)

            t = threading.Thread(target=_reindex, daemon=True, name="shmulsidian-reindex")
            t.start()
            self._prefetch_thread = t

            # Prefetch MEMORY.md content for system prompt
            self._prefetch_memory_index()

            logger.debug("shmulsidian initialized for session %s, vault=%s", session_id, self._vault)

        except Exception as e:
            logger.warning("shmulsidian init failed: %s", e)
            self._initialized = False

    def _prefetch_memory_index(self) -> None:
        """Read MEMORY.md and cache for system prompt injection."""
        if not self._vault:
            return
        memory_file = self._vault / "MEMORY.md"
        if memory_file.is_file():
            try:
                content = memory_file.read_text(encoding="utf-8", errors="replace")
                # Truncate to reasonable size for system prompt
                if len(content) > 4000:
                    content = content[:4000] + "\n\n[... truncated — use shmulsidian_search for full vault search]"
                with self._prefetch_lock:
                    self._prefetch_result = content
            except Exception as e:
                logger.debug("shmulsidian MEMORY.md read failed: %s", e)

    def system_prompt_block(self) -> str:
        """Inject MEMORY.md index into system prompt."""
        if not self._initialized:
            return ""
        with self._prefetch_lock:
            content = self._prefetch_result
        if not content:
            return ""
        return (
            "## Shmulsidian Vault (Obsidian Knowledge Base)\n\n"
            "The user maintains an Obsidian vault with structured notes, projects, "
            "and a Zettelkasten. Use `shmulsidian_search` to find relevant context, "
            "`shmulsidian_read` to read specific notes, and `shmulsidian_create` to "
            "save new notes.\n\n"
            "### Vault Index (MEMORY.md)\n\n"
            f"{content}"
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Search vault for context relevant to the current turn."""
        if not self._initialized or not self._index:
            return ""

        def _search():
            try:
                self._index.reindex()
                results = self._index.search_hybrid(query, k=5)
                if not results:
                    return ""
                lines = ["[Shmulsidian Context — relevant vault notes]"]
                seen_paths = set()
                for r in results:
                    if r["path"] not in seen_paths:
                        seen_paths.add(r["path"])
                        chunk_preview = r["chunk"][:200].replace("\n", " ")
                        lines.append(f"- {r['path']} (score: {r['score']:.2f}): {chunk_preview}")
                return "\n".join(lines)
            except Exception as e:
                logger.debug("shmulsidian prefetch failed: %s", e)
                return ""

        # Run search in background, return cached result
        result = _search()
        return result

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Track turn count for periodic re-indexing."""
        self._turn_count += 1
        # Re-index every 10 turns to pick up new notes
        if self._turn_count % 10 == 0 and self._index:
            def _reindex():
                try:
                    self._index.reindex()
                except Exception:
                    pass
            threading.Thread(target=_reindex, daemon=True).start()

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Save a session summary note to 00_Inbox on session end."""
        if not self._vault or not messages:
            return

        try:
            # Build a concise summary of the session
            user_msgs = [m for m in messages if m.get("role") == "user"]
            if not user_msgs:
                return

            first_msg = user_msgs[0].get("content", "")
            if isinstance(first_msg, list):
                first_msg = " ".join(
                    p.get("text", "") for p in first_msg if isinstance(p, dict)
                )[:200]

            title = f"Session {self._session_id}"
            body_lines = [f"Session ID: {self._session_id}", ""]
            body_lines.append("## Key Messages")
            for m in user_msgs[:5]:
                content = m.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        p.get("text", "") for p in content if isinstance(p, dict)
                    )
                if content:
                    body_lines.append(f"- {content[:200]}")

            _create_note_file(
                self._vault,
                title=title,
                body="\n".join(body_lines),
                folder="00_Inbox",
                tags=["session", "hermes"],
            )
            # Trigger re-index
            if self._index:
                self._index.reindex()
        except Exception as e:
            logger.debug("shmulsidian session save failed: %s", e)

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Mirror built-in memory writes to vault notes."""
        if not self._vault:
            return
        try:
            tag = "memory" if target == "memory" else "user-profile"
            _create_note_file(
                self._vault,
                title=f"Memory: {content[:60]}",
                body=f"Action: {action}\nTarget: {target}\n\n{content}",
                folder="00_Inbox",
                tags=["memory", tag, "hermes"],
            )
        except Exception as e:
            logger.debug("shmulsidian memory mirror failed: %s", e)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return ALL_SCHEMAS

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if not self._vault:
            return json.dumps({"success": False, "error": "Vault not initialized"})

        try:
            if tool_name == "shmulsidian_search":
                query = args.get("query", "")
                k = args.get("k", 10)
                if self._index:
                    self._index.reindex()
                    results = self._index.search_hybrid(query, k)
                else:
                    # Fallback: basic grep
                    results = self._grep_search(query, k)
                return json.dumps({"success": True, "results": results})

            elif tool_name == "shmulsidian_read":
                path = args.get("path", "")
                note = _read_note_file(self._vault, path)
                return json.dumps({"success": True, **note})

            elif tool_name == "shmulsidian_create":
                title = args.get("title", "")
                body = args.get("body", "")
                folder = args.get("folder", "00_Inbox")
                tags = args.get("tags")
                result = _create_note_file(self._vault, title, body, folder, tags)
                if self._index:
                    self._index.reindex()
                return json.dumps({"success": True, **result})

            elif tool_name == "shmulsidian_list":
                folder = args.get("folder")
                limit = args.get("limit", 20)
                notes = _list_notes(self._vault, folder, limit)
                return json.dumps({"success": True, "notes": notes})

            else:
                return json.dumps({"success": False, "error": f"Unknown tool: {tool_name}"})

        except Exception as e:
            return json.dumps({"success": False, "error": str(e)})

    def _grep_search(self, query: str, k: int) -> list[dict]:
        """Fallback search when index is unavailable."""
        results = []
        query_lower = query.lower()
        for path, mtime, size in _iter_md_files(self._vault):
            try:
                content = (self._vault / path).read_text(encoding="utf-8", errors="replace")
                if query_lower in content.lower():
                    # Find the matching chunk
                    idx = content.lower().find(query_lower)
                    start = max(0, idx - 100)
                    end = min(len(content), idx + 200)
                    chunk = content[start:end].replace("\n", " ")
                    results.append({"path": path, "chunk": chunk, "score": 1.0})
                    if len(results) >= k:
                        break
            except Exception:
                continue
        return results

    def shutdown(self) -> None:
        """Clean shutdown."""
        self._initialized = False
