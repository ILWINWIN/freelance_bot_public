"""Typed domain models shared by UI, adapters and the scheduler."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AccountKind(StrEnum):
    MARKETPLACE = "marketplace"
    TELEGRAM_BOT = "telegram_bot"
    TELEGRAM_USER = "telegram_user"


class ProposalMode(StrEnum):
    NOTIFY = "notify"
    DRAFT = "draft"
    AUTO = "auto"


class JobStatus(StrEnum):
    PENDING = "pending"
    PREPARING = "preparing"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    RUNNING = "running"
    SENDING = "sending"
    SENT = "sent"
    UNKNOWN_RESULT = "unknown_result"
    SUCCEEDED = "succeeded"  # Compatibility with early local databases.
    FAILED = "failed"
    PAUSED = "paused"
    CANCELLED = "cancelled"


class OrderStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    DISMISSED = "dismissed"


class OrderClassification(StrEnum):
    """Result of applying saved search rules without changing archive state."""

    MATCHED = "matched"
    REJECTED = "rejected"
    UNDETERMINED = "undetermined"


class ProcessState(StrEnum):
    OFF = "off"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING = "waiting"
    ERROR = "error"
    ATTENTION_REQUIRED = "attention_required"


@dataclass(slots=True)
class TelegramSource:
    chat_id: str
    name: str = ""
    enabled: bool = True
    language: str = "ru"
    poll_interval_seconds: int = 120
    pending_connection: bool = False
    reference: str = ""
    access_mode: str = "bot"
    access_status: str = "available"
    source_id: str = field(default="", compare=False)


@dataclass(frozen=True, slots=True)
class TelegramMessageEnvelope:
    """Transport-neutral Telegram facts consumed by the common order pipeline."""

    chat_id: str
    message_id: str
    text: str
    chat_title: str = ""
    chat_type: str = ""
    chat_username: str = ""
    published_at: datetime | None = None
    author_user_id: int | None = None
    author_username: str = ""
    author_name: str = ""
    author_is_anonymous: bool = False
    author_entity_type: str = ""


@dataclass(slots=True)
class Platform:
    id: str
    name: str
    adapter: str
    enabled: bool = True


@dataclass(slots=True)
class RateLimit:
    min_delay_seconds: float = 30.0
    max_delay_seconds: float = 90.0
    hourly_limit: int = 10
    daily_limit: int = 50
    error_pause_seconds: int = 900
    max_retries: int = 1


@dataclass(slots=True)
class Account:
    id: str
    platform_id: str
    name: str
    kind: AccountKind = AccountKind.MARKETPLACE
    enabled: bool = True
    requires_attention: bool = False
    last_checked_at: datetime | None = None
    rate_limit: RateLimit = field(default_factory=RateLimit)


@dataclass(slots=True)
class ServiceCategory:
    """Editable local service direction with human-friendly rule importance."""

    name: str
    enabled: bool = True
    rules: list[str] = field(default_factory=list)
    important_rules: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SearchProfile:
    id: str
    name: str
    include_keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    min_budget: float | None = None
    max_budget: float | None = None
    categories: list[str] = field(default_factory=list)
    order_signals: list[str] = field(default_factory=list)
    vacancy_signals: list[str] = field(default_factory=list)
    provider_ad_signals: list[str] = field(default_factory=list)
    stop_phrases: list[str] = field(default_factory=list)
    service_categories: list[ServiceCategory] = field(default_factory=list)
    telegram_hard_stops: list[str] = field(default_factory=list)
    telegram_order_signals: list[str] = field(default_factory=list)
    telegram_gate_configured: bool = False


@dataclass(slots=True)
class ProposalProfile:
    id: str
    name: str
    language: str = "ru"
    services: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    experience: str = ""
    style_examples: list[str] = field(default_factory=list)
    rules: str = ""
    forbidden_phrases: list[str] = field(default_factory=list)
    max_length: int = 1200
    minimum_price: float | None = None
    default_deadline: str = ""


@dataclass(slots=True)
class Order:
    id: str
    platform_id: str
    external_id: str
    title: str
    description: str
    url: str
    budget: float | None = None
    currency: str = ""
    category: str = ""
    discovered_at: datetime = field(default_factory=utc_now)
    match_score: float = 0.0
    status: OrderStatus = OrderStatus.ACTIVE
    # Existing locally stored orders predate rule classification. Keeping their
    # compatible default visible avoids moving a user's whole history away.
    classification: OrderClassification = OrderClassification.MATCHED
    published_at: datetime | None = None
    last_checked_at: datetime | None = None
    archived_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    content_fingerprint: str = ""

    @property
    def collected_at(self) -> datetime:
        """Expose the original collection timestamp under its lifecycle name."""
        return self.discovered_at

    @collected_at.setter
    def collected_at(self, value: datetime) -> None:
        self.discovered_at = value


@dataclass(slots=True)
class DispatchJob:
    id: str
    order_id: str
    account_id: str
    mode: ProposalMode
    status: JobStatus = JobStatus.PENDING
    proposal_text: str = ""
    attempts: int = 0
    next_attempt_at: datetime = field(default_factory=utc_now)
    error: str = ""
    confirmed_at: datetime | None = None
    started_at: datetime | None = None
    idempotency_key: str = ""
    created_at: datetime = field(default_factory=utc_now)
