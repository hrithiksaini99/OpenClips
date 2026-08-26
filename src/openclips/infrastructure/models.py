from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from openclips.domain.clips import ClipStatus
from openclips.domain.jobs import JobStatus
from openclips.domain.sources import SourceKind, SourceStatus


class Base(DeclarativeBase):
    pass


class JobRecord(Base):
    __tablename__ = "jobs"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False), default=JobStatus.QUEUED
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ClipRecord(Base):
    __tablename__ = "clips"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    source_path: Mapped[str] = mapped_column(String(1024))
    status: Mapped[ClipStatus] = mapped_column(
        Enum(ClipStatus, native_enum=False), default=ClipStatus.GENERATING
    )
    output_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SourceAssetRecord(Base):
    __tablename__ = "source_assets"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    source_kind: Mapped[SourceKind] = mapped_column(
        Enum(SourceKind, native_enum=False), default=SourceKind.LOCAL_UPLOAD
    )
    original_locator: Mapped[str] = mapped_column(String(2048))
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(255))
    media_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    byte_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[SourceStatus] = mapped_column(
        Enum(SourceStatus, native_enum=False), default=SourceStatus.PENDING
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retain_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TranscriptRecord(Base):
    __tablename__ = "transcripts"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    source_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_assets.id"), unique=True, index=True
    )
    language: Mapped[str] = mapped_column(String(32))
    duration: Mapped[float] = mapped_column(Float)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
