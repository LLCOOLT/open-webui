"""Lorebooks: standalone world-info books attached to character models and chats."""

from __future__ import annotations

import time
import uuid

from open_webui.internal.db import Base, get_async_db_context
from open_webui.models.access_grants import AccessGrantModel, AccessGrants
from open_webui.models.groups import Groups
from open_webui.models.users import User, UserModel, UserResponse
from open_webui.utils.roleplay.lorebook import normalize_lorebook
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import JSON, BigInteger, Column, Text, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

####################
# Lorebook DB Schema
####################


class Lorebook(Base):
    __tablename__ = 'lorebook'

    id = Column(Text, primary_key=True, unique=True)
    user_id = Column(Text)

    name = Column(Text)
    description = Column(Text, nullable=True)
    # {scan_depth, token_budget, recursive_scanning, extensions, entries: [...]}
    data = Column(JSON, nullable=True)
    # {tags, source, spec_version}
    meta = Column(JSON, nullable=True)

    created_at = Column(BigInteger)
    updated_at = Column(BigInteger)


def normalize_lorebook_data(data: dict | None) -> dict:
    """The stored ``data`` shape: book settings plus normalized entries (name/description live in columns)."""
    book = normalize_lorebook(data or {}) or {}
    return {key: value for key, value in book.items() if key not in ('name', 'description')}


class LorebookModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str

    name: str
    description: str | None = None
    data: dict = Field(default_factory=dict)
    meta: dict | None = None

    access_grants: list[AccessGrantModel] = Field(default_factory=list)

    created_at: int
    updated_at: int

    @field_validator('data', mode='before')
    @classmethod
    def normalize_data(cls, data):
        return normalize_lorebook_data(data)

    def as_book(self) -> dict:
        """The book as the activation engine and card export expect it."""
        return {'name': self.name, 'description': self.description or '', **self.data}


####################
# Forms
####################


class LorebookForm(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    description: str | None = None
    data: dict = Field(default_factory=dict)
    meta: dict | None = None
    access_grants: list[dict] | None = None

    @field_validator('data', mode='before')
    @classmethod
    def normalize_data(cls, data):
        return normalize_lorebook_data(data)


class LorebookUserResponse(LorebookModel):
    user: UserResponse | None = None


class LorebookListResponse(BaseModel):
    items: list[LorebookUserResponse]
    total: int


class LorebooksTable:
    async def _to_model(
        self, lorebook: Lorebook, access_grants: list[AccessGrantModel] | None = None, db: AsyncSession | None = None
    ) -> LorebookModel:
        model = LorebookModel.model_validate(lorebook)
        model.access_grants = (
            access_grants
            if access_grants is not None
            else await AccessGrants.get_grants_by_resource('lorebook', model.id, db=db)
        )
        return model

    def _has_permission(self, db, query, filter: dict, permission: str = 'read'):
        return AccessGrants.has_permission_filter(
            db=db,
            query=query,
            DocumentModel=Lorebook,
            filter=filter,
            resource_type='lorebook',
            permission=permission,
        )

    async def insert_new_lorebook(
        self, user_id: str, form_data: LorebookForm, db: AsyncSession | None = None
    ) -> LorebookModel:
        async with get_async_db_context(db) as db:
            now = int(time.time())
            lorebook = Lorebook(
                id=str(uuid.uuid4()),
                user_id=user_id,
                **form_data.model_dump(exclude={'access_grants'}),
                created_at=now,
                updated_at=now,
            )
            db.add(lorebook)
            await db.commit()
            await AccessGrants.set_access_grants('lorebook', lorebook.id, form_data.access_grants, db=db)
            return await self._to_model(lorebook, db=db)

    async def search_lorebooks(
        self,
        user_id: str | None,
        query: str | None = None,
        permission: str = 'read',
        skip: int = 0,
        limit: int | None = None,
        db: AsyncSession | None = None,
    ) -> LorebookListResponse:
        """Lorebooks ``user_id`` can access with ``permission``; every lorebook when ``user_id`` is None."""
        async with get_async_db_context(db) as db:
            stmt = select(Lorebook, User).outerjoin(User, User.id == Lorebook.user_id)
            if query:
                stmt = stmt.filter(or_(Lorebook.name.ilike(f'%{query}%'), Lorebook.description.ilike(f'%{query}%')))
            if user_id is not None:
                group_ids = [group.id for group in await Groups.get_groups_by_member_id(user_id, db=db)]
                stmt = self._has_permission(db, stmt, {'user_id': user_id, 'group_ids': group_ids}, permission)
            stmt = stmt.order_by(Lorebook.updated_at.desc())

            total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar()
            if skip:
                stmt = stmt.offset(skip)
            if limit:
                stmt = stmt.limit(limit)
            rows = (await db.execute(stmt)).all()

            grants = await AccessGrants.get_grants_by_resources('lorebook', [row[0].id for row in rows], db=db)
            items = [
                LorebookUserResponse(
                    **(await self._to_model(lorebook, access_grants=grants.get(lorebook.id, []), db=db)).model_dump(),
                    user=UserResponse(**UserModel.model_validate(owner).model_dump()) if owner else None,
                )
                for lorebook, owner in rows
            ]
            return LorebookListResponse(items=items, total=total)

    async def get_lorebook_by_id(self, id: str, db: AsyncSession | None = None) -> LorebookModel | None:
        async with get_async_db_context(db) as db:
            lorebook = (await db.execute(select(Lorebook).filter(Lorebook.id == id))).scalars().first()
            return await self._to_model(lorebook, db=db) if lorebook else None

    async def get_lorebooks_by_ids(self, ids: list[str], db: AsyncSession | None = None) -> list[LorebookModel]:
        if not ids:
            return []
        async with get_async_db_context(db) as db:
            rows = (await db.execute(select(Lorebook).filter(Lorebook.id.in_(ids)))).scalars().all()
            grants = await AccessGrants.get_grants_by_resources('lorebook', [row.id for row in rows], db=db)
            by_id = {row.id: await self._to_model(row, access_grants=grants.get(row.id, []), db=db) for row in rows}
            return [by_id[id] for id in ids if id in by_id]

    async def get_readable_lorebooks_by_ids(
        self, ids: list[str], user_id: str, db: AsyncSession | None = None
    ) -> list[LorebookModel]:
        """The lorebooks among ``ids`` that ``user_id`` owns or has read access to, in ``ids`` order."""
        lorebooks = await self.get_lorebooks_by_ids(ids, db=db)
        others = [lorebook.id for lorebook in lorebooks if lorebook.user_id != user_id]
        readable = set()
        if others:
            group_ids = {group.id for group in await Groups.get_groups_by_member_id(user_id, db=db)}
            readable = await AccessGrants.get_accessible_resource_ids(
                user_id=user_id,
                resource_type='lorebook',
                resource_ids=others,
                permission='read',
                user_group_ids=group_ids,
                db=db,
            )
        return [lorebook for lorebook in lorebooks if lorebook.user_id == user_id or lorebook.id in readable]

    async def update_lorebook_by_id(
        self, id: str, form_data: LorebookForm, db: AsyncSession | None = None
    ) -> LorebookModel | None:
        async with get_async_db_context(db) as db:
            lorebook = (await db.execute(select(Lorebook).filter(Lorebook.id == id))).scalars().first()
            if not lorebook:
                return None
            lorebook.name = form_data.name
            lorebook.description = form_data.description
            lorebook.data = form_data.data
            lorebook.meta = form_data.meta
            lorebook.updated_at = int(time.time())
            await db.commit()
            if form_data.access_grants is not None:
                await AccessGrants.set_access_grants('lorebook', id, form_data.access_grants, db=db)
            return await self._to_model(lorebook, db=db)

    async def delete_lorebook_by_id(self, id: str, db: AsyncSession | None = None) -> bool:
        async with get_async_db_context(db) as db:
            await AccessGrants.revoke_all_access('lorebook', id, db=db)
            result = await db.execute(delete(Lorebook).filter(Lorebook.id == id))
            await db.commit()
            return result.rowcount > 0


Lorebooks = LorebooksTable()
