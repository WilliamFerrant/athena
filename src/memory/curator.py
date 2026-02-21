"""Memory curation pipeline — sub-agents that process, tag, and maintain memories.

Instead of dumping raw conversation into mem0 and hoping search returns the
right thing, this module runs dedicated "librarian" sub-agents that:

1. **Categorize** — tag memories by type (fact, preference, decision, context, skill)
2. **Curate** — assess importance and merge duplicates
3. **Reinforce** — track usage frequency and boost important memories
4. **Archive** — move cold memories to archive tier (still searchable but lower priority)
5. **Evolve** — maintain historical chains (likes X → event → now dislikes X)

The curation runs asynchronously after each conversation turn, so it doesn't
block the user interaction.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class MemoryCategory(str, Enum):
    """Types of memories the librarian can classify."""
    FACT = "fact"                # Objective fact about user/project
    PREFERENCE = "preference"    # User likes/dislikes
    DECISION = "decision"        # Architectural or design decision
    CONTEXT = "context"          # Background context about a project
    SKILL = "skill"              # Technical skill or pattern learned
    RELATIONSHIP = "relationship"  # How things relate to each other
    EVENT = "event"              # Something that happened (temporal)


class MemoryTier(str, Enum):
    """Memory tiers for tiered context assembly."""
    HOT = "hot"          # Frequently accessed, always included
    WARM = "warm"        # Occasionally accessed, included when relevant
    COLD = "cold"        # Rarely accessed, archived but searchable
    ARCHIVED = "archived"  # Historical record, only for evolution chains


@dataclass
class CuratedMemory:
    """A memory with curation metadata."""
    id: str = ""
    content: str = ""
    category: MemoryCategory = MemoryCategory.FACT
    tier: MemoryTier = MemoryTier.WARM
    importance: float = 0.5        # 0.0–1.0
    access_count: int = 0
    last_accessed: float = 0.0
    created_at: float = field(default_factory=time.time)
    tags: list[str] = field(default_factory=list)
    supersedes: str | None = None  # ID of memory this evolved from
    agent_id: str = ""
    project_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "category": self.category.value,
            "tier": self.tier.value,
            "importance": self.importance,
            "access_count": self.access_count,
            "last_accessed": self.last_accessed,
            "created_at": self.created_at,
            "tags": self.tags,
            "supersedes": self.supersedes,
            "agent_id": self.agent_id,
            "project_id": self.project_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CuratedMemory:
        return cls(
            id=d.get("id", ""),
            content=d.get("content", ""),
            category=MemoryCategory(d.get("category", "fact")),
            tier=MemoryTier(d.get("tier", "warm")),
            importance=d.get("importance", 0.5),
            access_count=d.get("access_count", 0),
            last_accessed=d.get("last_accessed", 0.0),
            created_at=d.get("created_at", time.time()),
            tags=d.get("tags", []),
            supersedes=d.get("supersedes"),
            agent_id=d.get("agent_id", ""),
            project_id=d.get("project_id"),
        )


class MemoryCurator:
    """Curates memories using an LLM to categorize, merge, and evolve them.

    Works alongside AgentMemory — reads raw memories from mem0,
    enriches them with metadata stored in a local SQLite sidecar.
    """

    def __init__(
        self,
        llm_fn: Any = None,
        store: MemoryCurationStore | None = None,
    ) -> None:
        """
        Args:
            llm_fn: A callable(prompt: str) -> str for LLM calls (e.g. manager.chat)
            store: SQLite store for curation metadata
        """
        self._llm = llm_fn
        self.store = store or MemoryCurationStore()

    def categorize_memories(self, raw_memories: list[dict[str, Any]], agent_id: str, project_id: str | None = None) -> list[CuratedMemory]:
        """Categorize a batch of raw memories using the LLM.

        This runs as a batch operation — pass in memories that need
        categorization (e.g. newly added ones).
        """
        if not raw_memories or not self._llm:
            return []

        # Build a batch prompt
        memory_texts = []
        for i, mem in enumerate(raw_memories):
            text = mem.get("memory", mem.get("text", str(mem)))
            memory_texts.append(f"{i}: {text}")

        prompt = f"""Categorize each memory into exactly one category and assess its importance.

Categories: fact, preference, decision, context, skill, relationship, event
Importance: 0.0 (trivial) to 1.0 (critical)
Tags: 1-3 short topical tags

Memories:
{chr(10).join(memory_texts)}

Respond with a JSON array (no markdown fences):
[
  {{"index": 0, "category": "fact", "importance": 0.7, "tags": ["user-name", "personal"]}},
  ...
]"""

        try:
            raw = self._llm(prompt)
            start = raw.find("[")
            end = raw.rfind("]") + 1
            if start >= 0 and end > start:
                classifications = json.loads(raw[start:end])
            else:
                return []
        except Exception:
            logger.debug("Memory categorization LLM call failed")
            return []

        curated: list[CuratedMemory] = []
        for item in classifications:
            idx = item.get("index", -1)
            if 0 <= idx < len(raw_memories):
                mem = raw_memories[idx]
                mem_id = mem.get("id", "")
                content = mem.get("memory", mem.get("text", str(mem)))
                cm = CuratedMemory(
                    id=mem_id,
                    content=content,
                    category=MemoryCategory(item.get("category", "fact")),
                    importance=float(item.get("importance", 0.5)),
                    tags=item.get("tags", []),
                    agent_id=agent_id,
                    project_id=project_id,
                    created_at=mem.get("created_at", time.time()),
                )
                # Determine tier from importance
                if cm.importance >= 0.8:
                    cm.tier = MemoryTier.HOT
                elif cm.importance >= 0.4:
                    cm.tier = MemoryTier.WARM
                else:
                    cm.tier = MemoryTier.COLD

                curated.append(cm)
                self.store.upsert(cm)

        return curated

    def record_access(self, memory_id: str) -> None:
        """Record that a memory was accessed (for reinforcement learning)."""
        existing = self.store.get(memory_id)
        if existing:
            existing.access_count += 1
            existing.last_accessed = time.time()
            # Promote frequently accessed cold/warm memories
            if existing.access_count >= 5 and existing.tier == MemoryTier.COLD:
                existing.tier = MemoryTier.WARM
                logger.debug("Promoted memory %s from cold → warm", memory_id)
            elif existing.access_count >= 10 and existing.tier == MemoryTier.WARM:
                existing.tier = MemoryTier.HOT
                logger.debug("Promoted memory %s from warm → hot", memory_id)
            self.store.upsert(existing)

    def archive_cold_memories(self, agent_id: str, max_age_days: int = 7) -> int:
        """Move old, unused warm memories to cold tier."""
        cutoff = time.time() - (max_age_days * 86400)
        memories = self.store.list_by_tier(agent_id, MemoryTier.WARM)
        archived = 0
        for mem in memories:
            if mem.last_accessed < cutoff and mem.access_count < 3:
                mem.tier = MemoryTier.COLD
                self.store.upsert(mem)
                archived += 1
        if archived:
            logger.info("Archived %d cold memories for %s", archived, agent_id)
        return archived

    def evolve_memory(self, old_memory_id: str, new_content: str, agent_id: str, project_id: str | None = None) -> CuratedMemory:
        """Create an evolved version of a memory (historical chain).

        Example: "likes bees" → [stung by bee] → "hates bees"
        The old memory is archived, new one supersedes it.
        """
        old = self.store.get(old_memory_id)
        if old:
            old.tier = MemoryTier.ARCHIVED
            self.store.upsert(old)

        new_mem = CuratedMemory(
            id=f"evolved-{int(time.time()*1000)}",
            content=new_content,
            category=old.category if old else MemoryCategory.FACT,
            tier=MemoryTier.WARM,
            importance=old.importance if old else 0.5,
            tags=old.tags if old else [],
            supersedes=old_memory_id,
            agent_id=agent_id,
            project_id=project_id,
        )
        self.store.upsert(new_mem)
        return new_mem

    def get_evolution_chain(self, memory_id: str) -> list[CuratedMemory]:
        """Trace the historical evolution of a memory."""
        chain: list[CuratedMemory] = []
        current_id: str | None = memory_id
        visited: set[str] = set()
        while current_id and current_id not in visited:
            visited.add(current_id)
            mem = self.store.get(current_id)
            if mem:
                chain.append(mem)
                # Walk backwards via supersedes
                # Find what this memory superseded
                predecessor = self.store.find_superseded_by(current_id)
                current_id = predecessor.id if predecessor else None
            else:
                break
        return list(reversed(chain))

    def stats(self, agent_id: str) -> dict[str, Any]:
        """Get curation statistics for an agent."""
        all_mems = self.store.list_all(agent_id)
        by_tier = {t.value: 0 for t in MemoryTier}
        by_category = {c.value: 0 for c in MemoryCategory}
        for mem in all_mems:
            by_tier[mem.tier.value] = by_tier.get(mem.tier.value, 0) + 1
            by_category[mem.category.value] = by_category.get(mem.category.value, 0) + 1
        return {
            "total": len(all_mems),
            "by_tier": by_tier,
            "by_category": by_category,
            "hot_count": by_tier.get("hot", 0),
        }


# ---------------------------------------------------------------------------
# SQLite sidecar storage for curation metadata
# ---------------------------------------------------------------------------

import sqlite3
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent.parent / "data"
CURATION_DB = DATA_DIR / "memory_curation.db"


class MemoryCurationStore:
    """SQLite store for memory curation metadata.

    This is a sidecar to mem0 — mem0 stores the actual memory content
    and does vector search. This store adds categorization, tiers,
    access counts, and evolution chains on top.
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._db_path = str(db_path or CURATION_DB)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS curated_memories (
                    id            TEXT PRIMARY KEY,
                    content       TEXT NOT NULL DEFAULT '',
                    category      TEXT NOT NULL DEFAULT 'fact',
                    tier          TEXT NOT NULL DEFAULT 'warm',
                    importance    REAL NOT NULL DEFAULT 0.5,
                    access_count  INTEGER NOT NULL DEFAULT 0,
                    last_accessed REAL NOT NULL DEFAULT 0.0,
                    created_at    REAL NOT NULL,
                    tags          TEXT NOT NULL DEFAULT '[]',
                    supersedes    TEXT,
                    agent_id      TEXT NOT NULL,
                    project_id    TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_cm_agent_tier
                ON curated_memories (agent_id, tier)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_cm_supersedes
                ON curated_memories (supersedes)
            """)

    def upsert(self, mem: CuratedMemory) -> None:
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO curated_memories
                    (id, content, category, tier, importance, access_count,
                     last_accessed, created_at, tags, supersedes, agent_id, project_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    content=excluded.content, category=excluded.category,
                    tier=excluded.tier, importance=excluded.importance,
                    access_count=excluded.access_count, last_accessed=excluded.last_accessed,
                    tags=excluded.tags, supersedes=excluded.supersedes
            """, (
                mem.id, mem.content, mem.category.value, mem.tier.value,
                mem.importance, mem.access_count, mem.last_accessed,
                mem.created_at, json.dumps(mem.tags), mem.supersedes,
                mem.agent_id, mem.project_id,
            ))

    def get(self, memory_id: str) -> CuratedMemory | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM curated_memories WHERE id = ?", (memory_id,)
            ).fetchone()
        if not row:
            return None
        return self._row_to_curated(dict(row))

    def list_by_tier(self, agent_id: str, tier: MemoryTier, project_id: str | None = None) -> list[CuratedMemory]:
        with self._conn() as conn:
            if project_id:
                rows = conn.execute(
                    "SELECT * FROM curated_memories WHERE agent_id = ? AND tier = ? AND project_id = ? ORDER BY importance DESC",
                    (agent_id, tier.value, project_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM curated_memories WHERE agent_id = ? AND tier = ? ORDER BY importance DESC",
                    (agent_id, tier.value),
                ).fetchall()
        return [self._row_to_curated(dict(r)) for r in rows]

    def list_all(self, agent_id: str, project_id: str | None = None) -> list[CuratedMemory]:
        with self._conn() as conn:
            if project_id:
                rows = conn.execute(
                    "SELECT * FROM curated_memories WHERE agent_id = ? AND project_id = ? ORDER BY importance DESC",
                    (agent_id, project_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM curated_memories WHERE agent_id = ? ORDER BY importance DESC",
                    (agent_id,),
                ).fetchall()
        return [self._row_to_curated(dict(r)) for r in rows]

    def find_superseded_by(self, memory_id: str) -> CuratedMemory | None:
        """Find the memory whose 'supersedes' points to memory_id (predecessor)."""
        # Actually we want to find the memory that `memory_id` supersedes
        mem = self.get(memory_id)
        if mem and mem.supersedes:
            return self.get(mem.supersedes)
        return None

    def close(self) -> None:
        pass

    @staticmethod
    def _row_to_curated(row: dict[str, Any]) -> CuratedMemory:
        tags = row.get("tags", "[]")
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except Exception:
                tags = []
        return CuratedMemory(
            id=row["id"],
            content=row.get("content", ""),
            category=MemoryCategory(row.get("category", "fact")),
            tier=MemoryTier(row.get("tier", "warm")),
            importance=row.get("importance", 0.5),
            access_count=row.get("access_count", 0),
            last_accessed=row.get("last_accessed", 0.0),
            created_at=row.get("created_at", 0.0),
            tags=tags,
            supersedes=row.get("supersedes"),
            agent_id=row.get("agent_id", ""),
            project_id=row.get("project_id"),
        )
