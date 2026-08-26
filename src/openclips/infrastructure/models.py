from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Enum, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from openclips.domain.clips import ClipStatus
from openclips.domain.jobs import JobStatus


class Base(DeclarativeBase):
    pass


class JobRecord(Base):
    __tablename__ = "jobs"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(64))
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
