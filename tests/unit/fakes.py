import asyncio
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Self
from uuid import UUID

from scribe.application.inbox import EventHandler, InboundEvent
from scribe.application.interfaces import (
    ExportRepository,
    MediaInfo,
    MediaProbe,
    ObjectModifiedError,
    ObjectStorage,
    PasswordHasher,
    PlanRepository,
    ProjectRepository,
    RateLimitDecision,
    RateLimiter,
    RecordingRepository,
    RefreshTokenStore,
    StageRepository,
    StoredObject,
    UnitOfWork,
    UsageRepository,
    UserRepository,
)
from scribe.application.messaging import InboxRepository
from scribe.application.pagination import Cursor
from scribe.application.pipeline_ports import PipelineDispatcher, Transcriber, TranscriptIndex
from scribe.application.progress import Message, ProgressPublisher
from scribe.application.read_ports import IndexedSegment, SearchHit, TranscriptReader
from scribe.application.webhooks import WebhookDelivery, WebhookSender
from scribe.domain.errors import EmailTakenError, InvalidMediaError
from scribe.domain.events import DomainEvent
from scribe.domain.exports import Export, ExportFormat
from scribe.domain.pipeline import PIPELINE_STAGES, ProcessingStage, StageName, StageStatus
from scribe.domain.projects import Project
from scribe.domain.recordings import Recording, RecordingStatus
from scribe.domain.transcript import Segment, Summary, Transcription
from scribe.domain.users import Plan, User

FREE_PLAN = Plan(id=1, name="free", monthly_minutes=300, max_file_mb=500)


@dataclass
class FakeDb:
    """Shared in-memory state; every FakeUnitOfWork works on the same instance."""

    users: dict[UUID, User] = field(default_factory=dict)
    plans: dict[str, Plan] = field(default_factory=lambda: {"free": FREE_PLAN})
    projects: dict[UUID, Project] = field(default_factory=dict)
    recordings: dict[UUID, Recording] = field(default_factory=dict)
    stages: dict[tuple[UUID, StageName], ProcessingStage] = field(default_factory=dict)
    usage: dict[tuple[UUID, str], int] = field(default_factory=dict)
    events: list[DomainEvent] = field(default_factory=list)  # committed events
    inbox: set[tuple[UUID, str]] = field(default_factory=set)  # committed inbox keys
    exports: dict[tuple[UUID, ExportFormat], Export] = field(default_factory=dict)


def _paginate(items: list[Any], cursor: Cursor | None, limit: int) -> list[Any]:
    ordered = sorted(items, key=lambda i: (i.created_at, i.id), reverse=True)
    if cursor is not None:
        ordered = [i for i in ordered if (i.created_at, i.id) < (cursor.created_at, cursor.id)]
    return ordered[: limit + 1]


class InMemoryUserRepository(UserRepository):
    def __init__(self, users: dict[UUID, User]) -> None:
        self._users = users

    async def add(self, user: User) -> None:
        if any(u.email == user.email for u in self._users.values()):
            raise EmailTakenError
        self._users[user.id] = user

    async def get_by_id(self, user_id: UUID) -> User | None:
        return self._users.get(user_id)

    async def get_by_email(self, email: str) -> User | None:
        return next((u for u in self._users.values() if u.email == email), None)


class InMemoryPlanRepository(PlanRepository):
    def __init__(self, plans: dict[str, Plan]) -> None:
        self._plans = plans

    async def get_by_name(self, name: str) -> Plan | None:
        return self._plans.get(name)


class InMemoryProjectRepository(ProjectRepository):
    def __init__(self, projects: dict[UUID, Project]) -> None:
        self._projects = projects

    def _alive(self, owner_id: UUID) -> list[Project]:
        return [
            p for p in self._projects.values() if p.owner_id == owner_id and p.deleted_at is None
        ]

    async def add(self, project: Project) -> None:
        self._projects[project.id] = project

    async def get(self, project_id: UUID, owner_id: UUID) -> Project | None:
        return next((p for p in self._alive(owner_id) if p.id == project_id), None)

    async def get_by_name(self, owner_id: UUID, name: str) -> Project | None:
        return next((p for p in self._alive(owner_id) if p.name == name), None)

    async def list_by_owner(
        self, owner_id: UUID, *, limit: int, cursor: Cursor | None
    ) -> list[Project]:
        return _paginate(self._alive(owner_id), cursor, limit)

    async def save(self, project: Project) -> None:
        self._projects[project.id] = project


class InMemoryRecordingRepository(RecordingRepository):
    def __init__(self, recordings: dict[UUID, Recording]) -> None:
        self._recordings = recordings

    def _alive(self, owner_id: UUID) -> list[Recording]:
        return [
            r
            for r in self._recordings.values()
            if r.owner_id == owner_id and r.status is not RecordingStatus.DELETED
        ]

    async def add(self, recording: Recording) -> None:
        self._recordings[recording.id] = recording

    async def get(
        self, recording_id: UUID, owner_id: UUID, *, for_update: bool = False
    ) -> Recording | None:
        return next((r for r in self._alive(owner_id) if r.id == recording_id), None)

    async def list_by_owner(
        self,
        owner_id: UUID,
        *,
        project_id: UUID | None,
        status: RecordingStatus | None,
        limit: int,
        cursor: Cursor | None,
    ) -> list[Recording]:
        items = [
            r
            for r in self._alive(owner_id)
            if (project_id is None or r.project_id == project_id)
            and (status is None or r.status is status)
        ]
        return _paginate(items, cursor, limit)

    async def filter_visible_ids(
        self, owner_id: UUID, recording_ids: Collection[UUID], project_id: UUID | None
    ) -> set[UUID]:
        wanted = set(recording_ids)
        return {
            r.id
            for r in self._alive(owner_id)
            if r.id in wanted
            and r.status is RecordingStatus.COMPLETED
            and (project_id is None or r.project_id == project_id)
        }

    async def purge(self, recording_id: UUID) -> bool:
        recording = self._recordings.get(recording_id)
        if recording is None or recording.status is not RecordingStatus.DELETED:
            return False
        del self._recordings[recording_id]
        return True

    async def save(self, recording: Recording) -> None:
        self._recordings[recording.id] = recording


class InMemoryStageRepository(StageRepository):
    def __init__(self, stages: dict[tuple[UUID, StageName], ProcessingStage]) -> None:
        self._stages = stages

    async def reset_incomplete(self, recording_id: UUID) -> None:
        for name in PIPELINE_STAGES:
            stage = self._stages.get((recording_id, name))
            if stage is None:
                self._stages[(recording_id, name)] = ProcessingStage(recording_id, name)
            elif stage.status is not StageStatus.COMPLETED:
                stage.status = StageStatus.PENDING
                stage.progress = 0
                stage.error = None
                stage.started_at = None
                stage.finished_at = None

    async def get(self, recording_id: UUID, name: StageName) -> ProcessingStage | None:
        return self._stages.get((recording_id, name))

    async def list_for_recording(self, recording_id: UUID) -> list[ProcessingStage]:
        found = [
            self._stages[(recording_id, n)]
            for n in PIPELINE_STAGES
            if (recording_id, n) in self._stages
        ]
        return found

    async def save(self, stage: ProcessingStage) -> None:
        self._stages[(stage.recording_id, stage.name)] = stage


class InMemoryUsageRepository(UsageRepository):
    def __init__(self, usage: dict[tuple[UUID, str], int]) -> None:
        self._usage = usage

    async def lock_minutes(self, user_id: UUID, period: str) -> int:
        return self._usage.setdefault((user_id, period), 0)

    async def add_minutes(self, user_id: UUID, period: str, minutes: int) -> None:
        self._usage[(user_id, period)] = self._usage.get((user_id, period), 0) + minutes

    async def get_minutes(self, user_id: UUID, period: str) -> int:
        return self._usage.get((user_id, period), 0)


class InMemoryInboxRepository(InboxRepository):
    """Registrations become visible only on commit, like in a real transaction."""

    def __init__(self, committed: set[tuple[UUID, str]]) -> None:
        self._committed = committed
        self._pending: set[tuple[UUID, str]] = set()

    async def register(self, message_id: UUID, consumer: str) -> bool:
        key = (message_id, consumer)
        if key in self._committed or key in self._pending:
            return False
        self._pending.add(key)
        return True

    async def mark_processed(self, message_id: UUID, consumer: str) -> None:
        return None

    def commit(self) -> None:
        self._committed |= self._pending
        self._pending.clear()

    def discard(self) -> None:
        self._pending.clear()


class InMemoryExportRepository(ExportRepository):
    def __init__(self, exports: dict[tuple[UUID, ExportFormat], Export]) -> None:
        self._exports = exports

    async def upsert(self, export: Export) -> None:
        existing = self._exports.get((export.recording_id, export.format))
        if existing is None:
            self._exports[(export.recording_id, export.format)] = export
        else:  # like ON CONFLICT DO UPDATE: the id of the existing row is kept
            existing.storage_key = export.storage_key
            existing.status = export.status

    async def get(self, recording_id: UUID, fmt: ExportFormat) -> Export | None:
        return self._exports.get((recording_id, fmt))


class FakeUnitOfWork(UnitOfWork):
    def __init__(self, db: FakeDb) -> None:
        super().__init__()
        self._db = db
        self.users = InMemoryUserRepository(db.users)
        self.plans = InMemoryPlanRepository(db.plans)
        self.projects = InMemoryProjectRepository(db.projects)
        self.recordings = InMemoryRecordingRepository(db.recordings)
        self.stages = InMemoryStageRepository(db.stages)
        self.usage = InMemoryUsageRepository(db.usage)
        self._inbox = InMemoryInboxRepository(db.inbox)
        self.exports = InMemoryExportRepository(db.exports)
        self.inbox = self._inbox

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._inbox.discard()  # no commit() means the inbox row is rolled back

    async def commit(self) -> None:
        self._db.events.extend(self.events)
        self.events.clear()
        self._inbox.commit()

    async def rollback(self) -> None:
        self.events.clear()
        self._inbox.discard()


class FakePasswordHasher(PasswordHasher):
    @property
    def dummy_hash(self) -> str:
        return "hashed::dummy"

    async def hash(self, password: str) -> str:
        return f"hashed::{password}"

    async def verify(self, password: str, hashed: str) -> bool:
        return hashed == f"hashed::{password}"


class InMemoryRefreshTokenStore(RefreshTokenStore):
    def __init__(self) -> None:
        self.items: dict[str, UUID] = {}

    async def save(self, jti: str, user_id: UUID, ttl_seconds: int) -> None:
        self.items[jti] = user_id

    async def consume(self, jti: str) -> UUID | None:
        return self.items.pop(jti, None)

    async def revoke(self, jti: str) -> None:
        self.items.pop(jti, None)


class FakeObjectStorage(ObjectStorage):
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], StoredObject] = {}
        self.blobs: dict[tuple[str, str], bytes] = {}
        self.overwrite_before_copy = False

    def put(self, bucket: str, key: str, size: int, etag: str = "etag-1") -> None:
        self.objects[(bucket, key)] = StoredObject(size=size, content_type="audio/wav", etag=etag)

    async def ensure_buckets(self) -> None:
        return None

    async def create_upload_url(self, bucket: str, key: str, ttl_seconds: int) -> str:
        return f"http://storage.test/{bucket}/{key}?ttl={ttl_seconds}"

    async def create_internal_read_url(self, bucket: str, key: str, ttl_seconds: int) -> str:
        return f"http://storage.internal/{bucket}/{key}"

    async def stat(self, bucket: str, key: str) -> StoredObject | None:
        return self.objects.get((bucket, key))

    async def upload_file(self, bucket: str, key: str, path: Path, content_type: str) -> None:
        self.blobs[(bucket, key)] = await asyncio.to_thread(path.read_bytes)

    async def download_file(self, bucket: str, key: str, path: Path) -> None:
        data = self.blobs.get((bucket, key))
        if data is None:
            raise FileNotFoundError(key)
        await asyncio.to_thread(path.write_bytes, data)

    async def put_bytes(self, bucket: str, key: str, data: bytes, content_type: str) -> None:
        self.blobs[(bucket, key)] = data

    async def get_bytes(self, bucket: str, key: str) -> bytes | None:
        return self.blobs.get((bucket, key))

    async def create_download_url(
        self, bucket: str, key: str, ttl_seconds: int, filename: str
    ) -> str:
        return f"http://storage.test/download/{bucket}/{key}?name={filename}"

    async def copy(
        self, src_bucket: str, src_key: str, dst_bucket: str, dst_key: str, *, expected_etag: str
    ) -> None:
        source = self.objects.get((src_bucket, src_key))
        if source is None:
            raise FileNotFoundError(src_key)
        if self.overwrite_before_copy:
            source = StoredObject(
                size=source.size, content_type=source.content_type, etag="replaced"
            )
            self.objects[(src_bucket, src_key)] = source
        if source.etag != expected_etag:
            raise ObjectModifiedError(src_key)
        self.objects[(dst_bucket, dst_key)] = source

    async def delete_prefix(self, bucket: str, prefix: str) -> int:
        keys = {k for b, k in [*self.blobs, *self.objects] if b == bucket and k.startswith(prefix)}
        for key in keys:
            self.blobs.pop((bucket, key), None)
            self.objects.pop((bucket, key), None)
        return len(keys)


class FakeMediaProbe(MediaProbe):
    def __init__(self, duration_sec: float = 60.0, error: str | None = None) -> None:
        self.duration_sec = duration_sec
        self.error = error
        self.content_types: list[str] = []

    async def probe(self, source_url: str, content_type: str) -> MediaInfo:
        self.content_types.append(content_type)
        if self.error is not None:
            raise InvalidMediaError(self.error)
        return MediaInfo(duration_sec=self.duration_sec)


class CountingHandler(EventHandler):
    """Counts calls; the first `fail_times` calls raise, every call waits `delay` seconds."""

    def __init__(self, fail_times: int = 0, delay: float = 0.0) -> None:
        self.calls = 0
        self.fail_times = fail_times
        self.delay = delay

    async def handle(self, event: InboundEvent, uow: UnitOfWork) -> None:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.calls <= self.fail_times:
            raise RuntimeError("boom")


class FakeTranscriber(Transcriber):
    """Every call returns one segment `call N` at 0..1 s of the given audio."""

    def __init__(self, detected_language: str = "uk") -> None:
        self.calls: list[tuple[str, str | None]] = []
        self._detected = detected_language

    @property
    def model_name(self) -> str:
        return "fake"

    async def transcribe(self, audio_path: Path, language: str | None) -> Transcription:
        self.calls.append((audio_path.name, language))
        segment = Segment(0.0, 1.0, f"call {len(self.calls)}")
        return Transcription(language=language or self._detected, model="fake", segments=[segment])


class FakeTranscriptIndex(TranscriptIndex):
    def __init__(self) -> None:
        self.indexed: list[tuple[UUID, Transcription, Summary | None]] = []

    async def index(
        self,
        *,
        recording_id: UUID,
        owner_id: UUID,
        transcription: Transcription,
        summary: Summary | None,
    ) -> None:
        self.indexed.append((recording_id, transcription, summary))

    async def remove(self, recording_id: UUID) -> None:
        self.indexed = [item for item in self.indexed if item[0] != recording_id]


class FakeDispatcher(PipelineDispatcher):
    def __init__(self) -> None:
        self.dispatched: list[tuple[UUID, UUID, int]] = []

    async def dispatch(self, recording_id: UUID, owner_id: UUID, run: int) -> None:
        self.dispatched.append((recording_id, owner_id, run))


class FakeProgressPublisher(ProgressPublisher):
    def __init__(self) -> None:
        self.states: list[tuple[UUID, Message]] = []
        self.transient: list[tuple[UUID, Message]] = []

    async def publish_state(self, recording_id: UUID, message: Message) -> None:
        self.states.append((recording_id, message))

    async def publish_transient(self, recording_id: UUID, message: Message) -> None:
        self.transient.append((recording_id, message))


class FakeTranscriptReader(TranscriptReader):
    def __init__(self) -> None:
        self.segments: dict[UUID, list[Segment]] = {}
        self.summaries: dict[UUID, Summary] = {}
        self.scanned = 0

    async def list_segments(
        self, recording_id: UUID, owner_id: UUID, *, from_sec: float, after_idx: int, limit: int
    ) -> list[IndexedSegment]:
        rows = [
            IndexedSegment(idx, segment)
            for idx, segment in enumerate(self.segments.get(recording_id, []))
            if idx > after_idx and segment.end >= from_sec
        ]
        return rows[: limit + 1]

    async def get_summary(self, recording_id: UUID) -> Summary | None:
        return self.summaries.get(recording_id)

    async def search(self, owner_id: UUID, query: str, *, skip: int, limit: int) -> list[SearchHit]:
        terms = query.lower().split()
        hits = [
            SearchHit(recording_id, idx, segment, 1.0)
            for recording_id, segments in self.segments.items()
            for idx, segment in enumerate(segments)
            if any(term in segment.text.lower() for term in terms)
        ]
        page = hits[skip : skip + limit]
        self.scanned += len(page)
        return page


class FakeWebhookSender(WebhookSender):
    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[WebhookDelivery] = []
        self.error = error

    async def send(self, delivery: WebhookDelivery) -> None:
        if self.error is not None:
            raise self.error
        self.sent.append(delivery)


class FakeRateLimiter(RateLimiter):
    def __init__(
        self, allowed: bool = True, retry_after_sec: int = 7, error: Exception | None = None
    ) -> None:
        self.calls: list[tuple[str, int, int]] = []
        self._allowed = allowed
        self._retry_after = retry_after_sec
        self._error = error

    async def hit(self, key: str, limit: int, window_sec: int) -> RateLimitDecision:
        if self._error is not None:
            raise self._error
        self.calls.append((key, limit, window_sec))
        return RateLimitDecision(self._allowed, 0 if self._allowed else self._retry_after)
