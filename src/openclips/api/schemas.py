"""Request and response bodies for the OpenClips review API."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class SourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source_kind: str
    display_name: str
    status: str
    media_path: str | None
    byte_size: int | None
    external_id: str | None
    retain_until: datetime
    created_at: datetime


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    kind: str
    payload: str | None
    status: str
    attempts: int
    error: str | None
    created_at: datetime


class ClipOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source_asset_id: UUID | None
    title: str | None
    status: str
    start_time: float | None
    end_time: float | None
    selection_score: float | None
    output_path: str | None
    caption_path: str | None
    caption_template: str | None
    render_width: int | None
    render_height: int | None
    caption_edits: list[dict[str, str]] | None

    @property
    def has_render(self) -> bool:
        return self.output_path is not None


class ClipEditBody(BaseModel):
    title: str | None = None
    start_time: float | None = None
    end_time: float | None = None


class WordEditBody(BaseModel):
    match: str
    replacement: str


class CaptionEditsBody(BaseModel):
    edits: list[WordEditBody]


class BulkActionBody(BaseModel):
    action: Literal["approve", "reject"]
    clip_ids: list[UUID]


class BulkResultItem(BaseModel):
    clip_id: UUID
    ok: bool
    status: str | None = None
    error: str | None = None


class EnqueueJobOut(BaseModel):
    job_id: UUID
    kind: str
    status: str


class YouTubeIngestBody(BaseModel):
    url: str
    auto_process: bool = True


class SourceIngestOut(BaseModel):
    source: SourceOut
    next_job: EnqueueJobOut | None = None
