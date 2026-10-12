"""Character memory: what a character remembers about a user across their chats.

One row per (user, character model). Entries are short facts in the character's long-term memory of
that user; the roleplay recall code (``utils.roleplay.recall``) writes them and puts them into the
character's system block. Per user: what a character learns from one person never reaches another.
"""

from __future__ import annotations

import re
import time
import uuid

from open_webui.internal.db import Base, get_async_db_context
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import JSON, BigInteger, Column, Text, UniqueConstraint, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

MAX_ENTRIES = 40
MAX_ENTRY_LENGTH = 300


class CharacterMemory(Base):
    __tablename__ = 'character_memory'
    __table_args__ = (UniqueConstraint('user_id', 'model_id', name='uq_character_memory_user_model'),)

    id = Column(Text, primary_key=True, unique=True)
    user_id = Column(Text, nullable=False)
    model_id = Column(Text, nullable=False)
    # {'entries': [{'id', 'content', 'chat_id', 'created_at', 'updated_at'}]}
    data = Column(JSON, nullable=True)

    created_at = Column(BigInteger)
    updated_at = Column(BigInteger)


def _clean_content(value) -> str:
    if not isinstance(value, str):
        return ''
    return re.sub(r'\s+', ' ', value).strip()[:MAX_ENTRY_LENGTH].strip()


def normalize_entries(entries, chat_id: str | None = None, now: int | None = None) -> list[dict]:
    """Entries as stored: non-empty, deduplicated, at most ``MAX_ENTRIES``, each with an id and times.

    Accepts plain strings (new entries) and dicts (existing ones keep their id and times).
    """
    now = now if now is not None else int(time.time())
    cleaned: list[dict] = []
    seen: set[str] = set()
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, str):
            entry = {'content': entry}
        if not isinstance(entry, dict):
            continue
        content = _clean_content(entry.get('content'))
        key = content.lower()
        if not content or key in seen:
            continue
        seen.add(key)
        entry_id = entry.get('id')
        created = entry.get('created_at')
        cleaned.append(
            {
                'id': entry_id if isinstance(entry_id, str) and entry_id else uuid.uuid4().hex[:12],
                'content': content,
                'chat_id': entry.get('chat_id') if isinstance(entry.get('chat_id'), str) else chat_id,
                'created_at': created if isinstance(created, int) else now,
                'updated_at': entry.get('updated_at') if isinstance(entry.get('updated_at'), int) else now,
            }
        )
        if len(cleaned) >= MAX_ENTRIES:
            break
    return cleaned


class CharacterMemoryModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    model_id: str
    data: dict = Field(default_factory=dict)
    created_at: int
    updated_at: int

    @field_validator('data', mode='before')
    @classmethod
    def normalize_data(cls, data):
        data = data if isinstance(data, dict) else {}
        return {'entries': normalize_entries(data.get('entries'))}

    @property
    def entries(self) -> list[dict]:
        return self.data.get('entries') or []


class CharacterMemoriesTable:
    async def get_memory(
        self, user_id: str, model_id: str, db: AsyncSession | None = None
    ) -> CharacterMemoryModel | None:
        async with get_async_db_context(db) as db:
            row = (
                (await db.execute(select(CharacterMemory).filter_by(user_id=user_id, model_id=model_id)))
                .scalars()
                .first()
            )
            return CharacterMemoryModel.model_validate(row) if row else None

    async def get_entries(self, user_id: str, model_id: str, db: AsyncSession | None = None) -> list[dict]:
        memory = await self.get_memory(user_id, model_id, db=db)
        return memory.entries if memory else []

    async def set_entries(
        self, user_id: str, model_id: str, entries: list, chat_id: str | None = None, db: AsyncSession | None = None
    ) -> CharacterMemoryModel:
        """Replace the character's memory of this user. An empty list keeps the row, with no entries."""
        async with get_async_db_context(db) as db:
            now = int(time.time())
            row = (
                (await db.execute(select(CharacterMemory).filter_by(user_id=user_id, model_id=model_id)))
                .scalars()
                .first()
            )
            data = {'entries': normalize_entries(entries, chat_id=chat_id, now=now)}
            if row is None:
                row = CharacterMemory(
                    id=str(uuid.uuid4()), user_id=user_id, model_id=model_id, data=data, created_at=now, updated_at=now
                )
                db.add(row)
            else:
                row.data = data
                row.updated_at = now
            await db.commit()
            await db.refresh(row)
            return CharacterMemoryModel.model_validate(row)

    async def delete_memory(self, user_id: str, model_id: str, db: AsyncSession | None = None) -> bool:
        async with get_async_db_context(db) as db:
            result = await db.execute(delete(CharacterMemory).filter_by(user_id=user_id, model_id=model_id))
            await db.commit()
            return result.rowcount > 0

    async def delete_memories_by_model_id(self, model_id: str, db: AsyncSession | None = None) -> int:
        """Every user's memory of a character, for when the character is deleted."""
        async with get_async_db_context(db) as db:
            result = await db.execute(delete(CharacterMemory).filter_by(model_id=model_id))
            await db.commit()
            return result.rowcount

    async def delete_memories_by_user_id(self, user_id: str, db: AsyncSession | None = None) -> int:
        async with get_async_db_context(db) as db:
            result = await db.execute(delete(CharacterMemory).filter_by(user_id=user_id))
            await db.commit()
            return result.rowcount


CharacterMemories = CharacterMemoriesTable()
