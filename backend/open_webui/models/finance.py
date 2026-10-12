import logging
import time
from typing import Literal, Optional
from uuid import uuid4

from open_webui.internal.db import Base, get_async_db_context
from open_webui.utils.valves import decrypt_valves, encrypt_valves
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, BigInteger, Boolean, Column, Float, Index, Text, and_, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

# Asset classes the schema understands. Only 'us_equity' is enabled in phase 1;
# 'option' and 'crypto' are reserved so adapters and tables need no migration later.
AssetClass = Literal['us_equity', 'option', 'crypto']
ENABLED_ASSET_CLASSES: tuple[str, ...] = ('us_equity',)

####################
# Finance DB Schema
####################


class FinanceConnection(Base):
    __tablename__ = 'finance_connection'

    id = Column(Text, primary_key=True)
    user_id = Column(Text, nullable=False)
    name = Column(Text, nullable=False)
    provider = Column(Text, nullable=False)  # alpaca | edgar | (later) coinbase, massive, ...
    kind = Column(Text, nullable=False)  # broker | data
    asset_classes = Column(JSON, nullable=False)  # list[AssetClass]
    is_paper = Column(Boolean, nullable=False, default=True)
    is_default = Column(Boolean, nullable=False, default=False)
    credentials = Column(JSON, nullable=True)  # Fernet-encrypted via encrypt_valves
    meta = Column(JSON, nullable=True)

    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)

    __table_args__ = (Index('ix_finance_connection_user', 'user_id'),)


class FinanceBar(Base):
    """Local OHLCV cache keyed by provider/symbol/timeframe/timestamp (unix seconds)."""

    __tablename__ = 'finance_bar'

    provider = Column(Text, primary_key=True)
    symbol = Column(Text, primary_key=True)
    timeframe = Column(Text, primary_key=True)
    ts = Column(BigInteger, primary_key=True)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Float, nullable=True)
    vwap = Column(Float, nullable=True)


####################
# Pydantic Models
####################


class FinanceConnectionModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    name: str
    provider: str
    kind: str
    asset_classes: list[str]
    is_paper: bool
    is_default: bool
    meta: Optional[dict] = None

    created_at: int
    updated_at: int


class FinanceConnectionResponse(FinanceConnectionModel):
    has_credentials: bool = False
    key_hint: Optional[str] = None


class FinanceConnectionForm(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    provider: str
    kind: Literal['broker', 'data'] = 'broker'
    asset_classes: list[AssetClass] = Field(default_factory=lambda: ['us_equity'])
    is_paper: bool = True
    is_default: bool = False
    credentials: Optional[dict[str, str]] = None  # None on update keeps existing credentials
    meta: Optional[dict] = None


def _key_hint(credentials: dict) -> Optional[str]:
    key = credentials.get('api_key') or credentials.get('user_agent')
    if not key:
        return None
    return f'…{key[-4:]}' if len(key) > 4 else '…'


####################
# FinanceConnectionTable
####################


class FinanceConnectionTable:
    def _to_response(self, row: FinanceConnection) -> FinanceConnectionResponse:
        credentials = decrypt_valves(row.credentials)
        return FinanceConnectionResponse(
            **FinanceConnectionModel.model_validate(row).model_dump(),
            has_credentials=bool(credentials),
            key_hint=_key_hint(credentials),
        )

    async def _clear_default(self, db: AsyncSession, user_id: str, kind: str, except_id: str) -> None:
        await db.execute(
            update(FinanceConnection)
            .where(
                and_(
                    FinanceConnection.user_id == user_id,
                    FinanceConnection.kind == kind,
                    FinanceConnection.id != except_id,
                )
            )
            .values(is_default=False)
        )

    async def insert(
        self, user_id: str, form: FinanceConnectionForm, db: Optional[AsyncSession] = None
    ) -> FinanceConnectionResponse:
        async with get_async_db_context(db) as db:
            now = int(time.time_ns())
            # The first connection of a kind becomes the default automatically.
            existing = await db.execute(select(FinanceConnection.id).filter_by(user_id=user_id, kind=form.kind))
            is_default = form.is_default or existing.first() is None

            row = FinanceConnection(
                id=str(uuid4()),
                user_id=user_id,
                name=form.name,
                provider=form.provider,
                kind=form.kind,
                asset_classes=list(form.asset_classes),
                is_paper=form.is_paper,
                is_default=is_default,
                credentials=encrypt_valves(form.credentials or {}),
                meta=form.meta,
                created_at=now,
                updated_at=now,
            )
            db.add(row)
            if is_default:
                await self._clear_default(db, user_id, form.kind, row.id)
            await db.commit()
            return self._to_response(row)

    async def update_by_id(
        self, id: str, form: FinanceConnectionForm, db: Optional[AsyncSession] = None
    ) -> Optional[FinanceConnectionResponse]:
        async with get_async_db_context(db) as db:
            row = await db.get(FinanceConnection, id)
            if not row:
                return None
            row.name = form.name
            row.provider = form.provider
            row.kind = form.kind
            row.asset_classes = list(form.asset_classes)
            row.is_paper = form.is_paper
            row.is_default = form.is_default
            if form.credentials is not None:
                # Blank fields keep their stored value so the UI never needs to echo secrets.
                merged = decrypt_valves(row.credentials)
                merged.update({k: v for k, v in form.credentials.items() if v})
                row.credentials = encrypt_valves(merged)
            row.meta = form.meta
            row.updated_at = int(time.time_ns())
            if form.is_default:
                await self._clear_default(db, row.user_id, form.kind, row.id)
            await db.commit()
            return self._to_response(row)

    async def get_by_id(self, id: str, db: Optional[AsyncSession] = None) -> Optional[FinanceConnectionResponse]:
        async with get_async_db_context(db) as db:
            row = await db.get(FinanceConnection, id)
            return self._to_response(row) if row else None

    async def get_credentials_by_id(self, id: str, db: Optional[AsyncSession] = None) -> dict:
        async with get_async_db_context(db) as db:
            row = await db.get(FinanceConnection, id)
            return decrypt_valves(row.credentials) if row else {}

    async def get_by_user(
        self, user_id: str, kind: Optional[str] = None, db: Optional[AsyncSession] = None
    ) -> list[FinanceConnectionResponse]:
        async with get_async_db_context(db) as db:
            stmt = select(FinanceConnection).filter_by(user_id=user_id)
            if kind:
                stmt = stmt.filter_by(kind=kind)
            result = await db.execute(stmt.order_by(FinanceConnection.created_at.asc()))
            return [self._to_response(r) for r in result.scalars().all()]

    async def get_default(
        self, user_id: str, kind: str = 'broker', provider: Optional[str] = None, db: Optional[AsyncSession] = None
    ) -> Optional[FinanceConnectionResponse]:
        connections = await self.get_by_user(user_id, kind=kind, db=db)
        if provider:
            connections = [c for c in connections if c.provider == provider]
        for c in connections:
            if c.is_default:
                return c
        return connections[0] if connections else None

    async def delete_by_id(self, id: str, db: Optional[AsyncSession] = None) -> bool:
        async with get_async_db_context(db) as db:
            await db.execute(delete(FinanceConnection).filter_by(id=id))
            await db.commit()
            return True


class FinanceBarTable:
    async def get_range(
        self,
        provider: str,
        symbol: str,
        timeframe: str,
        start: int,
        end: int,
        db: Optional[AsyncSession] = None,
    ) -> list[dict]:
        async with get_async_db_context(db) as db:
            result = await db.execute(
                select(FinanceBar)
                .filter_by(provider=provider, symbol=symbol, timeframe=timeframe)
                .filter(FinanceBar.ts >= start, FinanceBar.ts <= end)
                .order_by(FinanceBar.ts.asc())
            )
            return [
                {'t': r.ts, 'o': r.open, 'h': r.high, 'l': r.low, 'c': r.close, 'v': r.volume, 'vw': r.vwap}
                for r in result.scalars().all()
            ]

    async def upsert_many(
        self, provider: str, symbol: str, timeframe: str, bars: list[dict], db: Optional[AsyncSession] = None
    ) -> None:
        if not bars:
            return
        async with get_async_db_context(db) as db:
            for b in bars:
                await db.merge(
                    FinanceBar(
                        provider=provider,
                        symbol=symbol,
                        timeframe=timeframe,
                        ts=b['t'],
                        open=b['o'],
                        high=b['h'],
                        low=b['l'],
                        close=b['c'],
                        volume=b.get('v'),
                        vwap=b.get('vw'),
                    )
                )
            await db.commit()


FinanceConnections = FinanceConnectionTable()
FinanceBars = FinanceBarTable()
