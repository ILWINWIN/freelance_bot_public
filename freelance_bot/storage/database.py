"""Small dependency-free SQLite store; the repository boundary supports a later PostgreSQL migration."""

import json
import sqlite3
from functools import wraps
from threading import RLock
from pathlib import Path
from datetime import datetime, timedelta, timezone
from freelance_bot.core.models import Account, AccountKind, DispatchJob, JobStatus, Order, OrderClassification, OrderStatus, ProposalMode, SearchProfile, TelegramSource


def serialized_store(cls):
    """Serialize public store operations across transport and desktop workers."""
    for name, method in list(vars(cls).items()):
        if name.startswith('_') or not callable(method):
            continue
        def guard(fn):
            @wraps(fn)
            def call(self, *args, **kwargs):
                with self._lock:
                    return fn(self, *args, **kwargs)
            return call
        setattr(cls, name, guard(method))
    return cls


@serialized_store
class Database:
    def __init__(self, path: str = "data/freelance_bot.sqlite3") -> None:
        self._lock = RLock()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.initialize()

    def initialize(self) -> None:
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            id TEXT PRIMARY KEY, platform_id TEXT NOT NULL, name TEXT NOT NULL,
            kind TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
            requires_attention INTEGER NOT NULL DEFAULT 0, secret_ciphertext TEXT
        );
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY, platform_id TEXT NOT NULL, external_id TEXT NOT NULL,
            title TEXT NOT NULL, description TEXT NOT NULL, url TEXT NOT NULL,
            budget REAL, payload TEXT NOT NULL DEFAULT '{}', UNIQUE(platform_id, external_id)
        );
        CREATE TABLE IF NOT EXISTS dispatch_jobs (
            id TEXT PRIMARY KEY, order_id TEXT NOT NULL, account_id TEXT NOT NULL,
            mode TEXT NOT NULL, status TEXT NOT NULL, proposal_text TEXT NOT NULL DEFAULT '',
            attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS action_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
            action TEXT NOT NULL, status TEXT NOT NULL, details TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS telegram_sources (
            chat_id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
            language TEXT NOT NULL DEFAULT 'ru', poll_interval_seconds INTEGER NOT NULL DEFAULT 120
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS order_tombstones (
            platform_id TEXT NOT NULL, external_id TEXT NOT NULL, archived_at TEXT NOT NULL,
            reason TEXT NOT NULL, PRIMARY KEY(platform_id, external_id)
        );
        CREATE TABLE IF NOT EXISTS desktop_drafts (
            order_id TEXT PRIMARY KEY, text TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS telegram_inbox (
            update_id INTEGER PRIMARY KEY, chat_id TEXT NOT NULL, payload TEXT NOT NULL,
            received_at TEXT NOT NULL, processed_at TEXT, error_code TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS telegram_source_state (
            chat_id TEXT PRIMARY KEY, last_processed_at TEXT, last_error_at TEXT,
            last_error_code TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS telegram_order_deliveries (
            order_id TEXT NOT NULL, recipient_id TEXT NOT NULL, status TEXT NOT NULL,
            telegram_message_id TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
            PRIMARY KEY(order_id, recipient_id)
        );
        CREATE TABLE IF NOT EXISTS telegram_message_aliases (
            platform_id TEXT NOT NULL, external_id TEXT NOT NULL, canonical_order_id TEXT NOT NULL,
            content_fingerprint TEXT NOT NULL, observed_at TEXT NOT NULL,
            PRIMARY KEY(platform_id, external_id)
        );
        CREATE TABLE IF NOT EXISTS order_deletion_queue (
            order_id TEXT PRIMARY KEY, requested_at TEXT NOT NULL, delete_after TEXT NOT NULL,
            batch_id TEXT NOT NULL, origin TEXT NOT NULL, last_error TEXT NOT NULL DEFAULT '',
            last_attempt_at TEXT, attempt_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_order_deletion_queue_deadline
            ON order_deletion_queue(delete_after);
        CREATE TABLE IF NOT EXISTS deleted_order_tombstones (
            platform_id TEXT NOT NULL, external_id TEXT NOT NULL, deleted_at TEXT NOT NULL,
            PRIMARY KEY(platform_id, external_id)
        );
        """)
        # SQLite deployments created by early versions are upgraded in place.
        # These fields distinguish active work from archived and uncertain work.
        self._ensure_column("orders", "status", "TEXT NOT NULL DEFAULT 'active'")
        self._ensure_column("orders", "published_at", "TEXT")
        self._ensure_column("orders", "last_checked_at", "TEXT")
        self._ensure_column("orders", "archived_at", "TEXT")
        self._ensure_column("orders", "currency", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("orders", "discovered_at", "TEXT")
        self._ensure_column("orders", "classification", "TEXT NOT NULL DEFAULT 'matched'")
        self._ensure_column("orders", "content_fingerprint", "TEXT NOT NULL DEFAULT ''")
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_telegram_content ON orders(platform_id, content_fingerprint, discovered_at) WHERE content_fingerprint<>''"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_status_discovered ON orders(status, discovered_at)"
        )
        self._ensure_column("telegram_sources", "pending_connection", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("telegram_sources", "reference", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("telegram_sources", "access_mode", "TEXT NOT NULL DEFAULT 'bot'")
        self._ensure_column("telegram_sources", "access_status", "TEXT NOT NULL DEFAULT 'available'")
        self._ensure_column("telegram_sources", "source_id", "TEXT NOT NULL DEFAULT ''")
        self.connection.execute(
            "UPDATE telegram_sources SET source_id='telegram-' || replace(replace(chat_id, '-', 'm'), ':', 'x') WHERE source_id=''"
        )
        self.connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_telegram_sources_source_id ON telegram_sources(source_id) WHERE source_id<>''"
        )
        self._ensure_column("telegram_source_state", "user_last_message_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("telegram_source_state", "history_synced_at", "TEXT")
        self._ensure_column("telegram_source_state", "initial_history_from_at", "TEXT")
        self._ensure_column("telegram_source_state", "initial_history_until_at", "TEXT")
        self._ensure_column("telegram_source_state", "retry_after", "TEXT")
        # A previous migration approximated missing discovery time with the
        # source publication time. Reset only those legacy estimates once;
        # publication time must never determine the 12-hour archive deadline.
        collection_migration = self.connection.execute(
            "SELECT value FROM settings WHERE key='collection_time_backfill_v1'"
        ).fetchone()
        if collection_migration is None:
            migration_time = datetime.now(timezone.utc).isoformat()
            self.connection.execute(
                """UPDATE orders SET discovered_at=?
                WHERE discovered_at IS NULL OR (published_at IS NOT NULL AND discovered_at=published_at)""",
                (migration_time,),
            )
            self.connection.execute(
                "INSERT INTO settings(key,value) VALUES('collection_time_backfill_v1',?)",
                (migration_time,),
            )
        self._ensure_column("dispatch_jobs", "confirmed_at", "TEXT")
        self._ensure_column("dispatch_jobs", "started_at", "TEXT")
        self._ensure_column("dispatch_jobs", "idempotency_key", "TEXT NOT NULL DEFAULT ''")
        self.connection.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in self.connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def save_secret(self, account_id: str, ciphertext: str) -> None:
        self.connection.execute("UPDATE accounts SET secret_ciphertext=? WHERE id=?", (ciphertext, account_id))
        self.connection.commit()

    def load_secret(self, account_id: str) -> str:
        """Read encrypted local credentials without exposing them to logs or UI."""
        row = self.connection.execute("SELECT secret_ciphertext FROM accounts WHERE id=?", (account_id,)).fetchone()
        return str(row["secret_ciphertext"] or "") if row else ""

    def delete_account(self, account_id: str) -> None:
        """Remove a locally stored account and its encrypted credentials."""
        self.connection.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        self.connection.commit()

    def save_order(self, order: Order) -> bool:
        """Insert an order once; repeats enrich facts without undoing user workflow state."""
        cursor = self.connection.execute(
            """INSERT OR IGNORE INTO orders
            (id, platform_id, external_id, title, description, url, budget, payload, status, classification, published_at, last_checked_at, archived_at, currency, discovered_at, content_fingerprint)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (order.id, order.platform_id, order.external_id, order.title, order.description,
             order.url, order.budget, json.dumps(order.metadata, ensure_ascii=False), order.status.value,
             order.classification.value, self._format_datetime(order.published_at),
             self._format_datetime(order.last_checked_at), self._format_datetime(order.archived_at),
             order.currency, self._format_datetime(order.discovered_at), order.content_fingerprint),
        )
        is_new = cursor.rowcount == 1
        if not is_new:
            existing = self.connection.execute(
                "SELECT * FROM orders WHERE platform_id=? AND external_id=?", (order.platform_id, order.external_id)
            ).fetchone()
            if existing is None:
                # A conflicting primary key with a different external identity is invalid input.
                self.connection.rollback()
                raise sqlite3.IntegrityError("Order ID conflicts with a different external message")
            previous = self._row_to_order(existing)
            metadata = self._merge_metadata(previous.metadata, order.metadata)
            self.connection.execute(
                """UPDATE orders SET url=CASE WHEN url='' THEN ? ELSE url END,
                payload=?, last_checked_at=? WHERE id=?""",
                (order.url, json.dumps(metadata, ensure_ascii=False), self._format_datetime(order.last_checked_at), previous.id),
            )
            self.connection.commit()
            return False
        self.connection.commit()
        return True

    def save_telegram_order(self, order: Order, fingerprint: str, recent_since: datetime) -> tuple[Order | None, bool]:
        """Atomically save a Telegram post or map an identical recent repost to its canonical order."""
        external_id = str(order.external_id)
        now = datetime.now(timezone.utc).isoformat()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            deleted = self.connection.execute(
                "SELECT 1 FROM deleted_order_tombstones WHERE platform_id='telegram' AND external_id=?",
                (external_id,),
            ).fetchone()
            if deleted is not None:
                self.connection.commit()
                return None, False
            existing = self.connection.execute(
                "SELECT * FROM orders WHERE platform_id='telegram' AND external_id=?", (external_id,)
            ).fetchone()
            if existing is not None:
                canonical = self._row_to_order(existing)
                self._enrich_existing_telegram_order(canonical, order, fingerprint)
                self._remember_telegram_alias(external_id, canonical.id, fingerprint, now)
                self.connection.commit()
                return canonical, False

            alias = self.connection.execute(
                "SELECT canonical_order_id FROM telegram_message_aliases WHERE platform_id='telegram' AND external_id=?",
                (external_id,),
            ).fetchone()
            if alias is not None:
                canonical_row = self.connection.execute(
                    "SELECT * FROM orders WHERE id=? AND platform_id='telegram'", (alias["canonical_order_id"],)
                ).fetchone()
                if canonical_row is not None:
                    self.connection.commit()
                    return self._row_to_order(canonical_row), False
                self.connection.execute(
                    "DELETE FROM telegram_message_aliases WHERE platform_id='telegram' AND external_id=?", (external_id,)
                )

            # Новый идентификатор сообщения может принадлежать репосту уже сохранённого заказа.
            duplicate = None
            if fingerprint:
                duplicate = self.connection.execute(
                    """SELECT * FROM orders WHERE platform_id='telegram' AND content_fingerprint=?
                    AND discovered_at>=? ORDER BY discovered_at,id LIMIT 1""",
                    (fingerprint, recent_since.isoformat()),
                ).fetchone()
            if duplicate is not None:
                canonical = self._row_to_order(duplicate)
                self._remember_telegram_alias(external_id, canonical.id, fingerprint, now)
                self.connection.commit()
                return canonical, False

            order.content_fingerprint = fingerprint
            self.connection.execute(
                """INSERT INTO orders
                (id, platform_id, external_id, title, description, url, budget, payload, status,
                classification, published_at, last_checked_at, archived_at, currency, discovered_at, content_fingerprint)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (order.id, order.platform_id, order.external_id, order.title, order.description, order.url,
                 order.budget, json.dumps(order.metadata, ensure_ascii=False), order.status.value,
                 order.classification.value, self._format_datetime(order.published_at),
                 self._format_datetime(order.last_checked_at), self._format_datetime(order.archived_at),
                 order.currency, self._format_datetime(order.discovered_at), fingerprint),
            )
            self._remember_telegram_alias(external_id, order.id, fingerprint, now)
            self.connection.commit()
            return order, True
        except Exception:
            self.connection.rollback()
            raise

    def _enrich_existing_telegram_order(self, existing: Order, incoming: Order, fingerprint: str) -> None:
        """Fill newly confirmed Telegram metadata without replacing classification or user state."""
        metadata = self._merge_metadata(existing.metadata, incoming.metadata)
        self.connection.execute(
            """UPDATE orders SET url=CASE WHEN url='' THEN ? ELSE url END, payload=?,
            last_checked_at=?, content_fingerprint=?
            WHERE id=?""",
            (incoming.url, json.dumps(metadata, ensure_ascii=False),
             self._format_datetime(incoming.last_checked_at), fingerprint, existing.id),
        )

    def _remember_telegram_alias(self, external_id: str, order_id: str, fingerprint: str, observed_at: str) -> None:
        """Persist exact post identity for canonical orders and cross-channel reposts."""
        self.connection.execute(
            """INSERT OR IGNORE INTO telegram_message_aliases
            (platform_id,external_id,canonical_order_id,content_fingerprint,observed_at)
            VALUES('telegram',?,?,?,?)""",
            (external_id, order_id, fingerprint, observed_at),
        )

    def telegram_orders_missing_fingerprint(self, since: datetime) -> list[Order]:
        """Return recent legacy Telegram orders that need a content fingerprint backfill."""
        rows = self.connection.execute(
            """SELECT * FROM orders WHERE platform_id='telegram' AND content_fingerprint=''
            AND discovered_at>=? ORDER BY discovered_at,id""", (since.isoformat(),)
        ).fetchall()
        return [self._row_to_order(row) for row in rows]

    def reserve_telegram_order_delivery(self, order_id: str, recipient_id: int | str) -> bool:
        """Atomically claim a single automatic delivery, including in-flight attempts."""
        cursor = self.connection.execute(
            """INSERT OR IGNORE INTO telegram_order_deliveries
            (order_id,recipient_id,status,updated_at) VALUES(?,?,'sending',?)""",
            (str(order_id), str(recipient_id), datetime.now(timezone.utc).isoformat()),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def complete_telegram_order_delivery(self, order_id: str, recipient_id: int | str, message_id: int | str = "") -> None:
        """Persist confirmed Bot API delivery and its message ID when available."""
        self.connection.execute(
            """UPDATE telegram_order_deliveries SET status='sent',telegram_message_id=?,updated_at=?
            WHERE order_id=? AND recipient_id=? AND status='sending'""",
            (str(message_id or ""), datetime.now(timezone.utc).isoformat(), str(order_id), str(recipient_id)),
        )
        self.connection.commit()

    def release_telegram_order_delivery(self, order_id: str, recipient_id: int | str) -> None:
        """Release only an explicitly rejected delivery so a later retry can proceed."""
        self.connection.execute(
            "DELETE FROM telegram_order_deliveries WHERE order_id=? AND recipient_id=? AND status='sending'",
            (str(order_id), str(recipient_id)),
        )
        self.connection.commit()

    def telegram_order_delivery(self, order_id: str, recipient_id: int | str) -> dict | None:
        """Read durable delivery status for one order and owner."""
        row = self.connection.execute(
            "SELECT * FROM telegram_order_deliveries WHERE order_id=? AND recipient_id=?",
            (str(order_id), str(recipient_id)),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _merge_metadata(previous: dict, incoming: dict) -> dict:
        """Keep known facts and fill only fields newly confirmed by another transport."""
        merged = dict(previous or {})
        for key, value in (incoming or {}).items():
            if value not in (None, "", [], {}):
                merged[key] = value
        return merged

    def get_order(self, order_id: str) -> Order | None:
        row = self.connection.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_order(row)

    def update_order_telegram_metadata(self, order_id: str, metadata: dict, url: str) -> None:
        """Enrich one existing Telegram order without changing its workflow state."""
        self.connection.execute(
            "UPDATE orders SET payload=?, url=? WHERE id=? AND platform_id='telegram'",
            (json.dumps(metadata, ensure_ascii=False), url, order_id),
        )
        self.connection.commit()

    def list_orders(
        self, limit: int = 10, status: OrderStatus | None = OrderStatus.ACTIVE,
        query: str = "", sort: str = "newest", classification: OrderClassification | None = None,
        since: datetime | None = None, before: tuple[datetime, str] | None = None,
    ) -> list[Order]:
        """Return feed items using a small, validated set of sort options."""
        clauses: list[str] = []
        values: list[object] = []
        if status is not None:
            clauses.append("status=?")
            values.append(status.value)
        if classification is not None:
            clauses.append("classification=?")
            values.append(classification.value)
        time_expression = "published_at" if sort == "published_newest" else "COALESCE(published_at, discovered_at, last_checked_at)"
        since_expression = "COALESCE(published_at, discovered_at, last_checked_at)"
        if since is not None:
            # Legacy posts may lack a source date; retain them in the window
            # using their historical discovery date, while new Telegram posts
            # are still ordered strictly by published_at below.
            clauses.append(f"{since_expression}>=?")
            values.append(self._format_datetime(since))
        if before is not None:
            before_time, before_id = before
            clauses.append(f"({time_expression}<? OR ({time_expression}=? AND id<?))")
            values.extend([self._format_datetime(before_time), self._format_datetime(before_time), before_id])
        if query.strip():
            clauses.append("(title LIKE ? OR description LIKE ? OR payload LIKE ?)")
            needle = f"%{query.strip()}%"
            values.extend([needle, needle, needle])
        order_by = {
            "newest": f"{time_expression} DESC, id DESC",
            "published_newest": "published_at DESC, id DESC",
            "budget_desc": "budget IS NULL, budget DESC, rowid DESC",
            "budget_asc": "budget IS NULL, budget ASC, rowid DESC",
            "source": "platform_id ASC, rowid DESC",
        }.get(sort, "COALESCE(published_at, last_checked_at) DESC, rowid DESC")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(limit)
        rows = self.connection.execute(f"SELECT * FROM orders{where} ORDER BY {order_by} LIMIT ?", values).fetchall()
        return [self._row_to_order(row) for row in rows]

    def list_recent_telegram_orders_for_delivery(self, collected_since: datetime, limit: int = 70) -> list[Order]:
        """Load recent, active, already matched Telegram orders for delivery recovery."""
        rows = self.connection.execute(
            """SELECT * FROM orders WHERE platform_id='telegram' AND status='active'
            AND classification='matched' AND discovered_at>=?
            AND NOT EXISTS (SELECT 1 FROM deleted_order_tombstones t
                WHERE t.platform_id=orders.platform_id AND t.external_id=orders.external_id)
            ORDER BY published_at DESC, id DESC LIMIT ?""",
            (self._format_datetime(collected_since), max(0, min(int(limit), 70))),
        ).fetchall()
        return [self._row_to_order(row) for row in rows]

    def list_recent_telegram_orders_for_ordered_delivery(self, collected_since: datetime, limit: int = 70) -> list[Order]:
        """Select the newest bounded restore batch, then return it oldest first."""
        rows = self.connection.execute(
            """SELECT * FROM (
                SELECT * FROM orders WHERE platform_id='telegram' AND status='active'
                AND classification='matched' AND discovered_at>=?
                AND NOT EXISTS (SELECT 1 FROM deleted_order_tombstones t
                    WHERE t.platform_id=orders.platform_id AND t.external_id=orders.external_id)
                ORDER BY published_at DESC, id DESC LIMIT ?
            ) AS latest
            ORDER BY published_at ASC, id ASC""",
            (self._format_datetime(collected_since), max(0, min(int(limit), 70))),
        ).fetchall()
        return [self._row_to_order(row) for row in rows]

    def telegram_runtime_metrics(self) -> dict[str, object]:
        """Return counts and timestamps used by the compact Telegram status command."""
        rows = self.connection.execute(
            """SELECT status,classification,COUNT(*) AS amount FROM orders
            WHERE platform_id='telegram' GROUP BY status,classification"""
        ).fetchall()
        active_matched = active_unknown = archived = 0
        for row in rows:
            if row["status"] == "active" and row["classification"] == "matched":
                active_matched += row["amount"]
            elif row["status"] == "active" and row["classification"] == "undetermined":
                active_unknown += row["amount"]
            elif row["status"] == "archived":
                archived += row["amount"]
        source_row = self.connection.execute(
            """SELECT COUNT(*) AS total,COALESCE(SUM(enabled),0) AS enabled,
            COALESCE(SUM(CASE WHEN state.last_error_code<>'' THEN 1 ELSE 0 END),0) AS errors,
            MAX(state.last_processed_at) AS last_post
            FROM telegram_sources AS source LEFT JOIN telegram_source_state AS state
            ON state.chat_id=source.chat_id"""
        ).fetchone()
        latest_order = self.connection.execute(
            """SELECT MAX(published_at) FROM orders WHERE platform_id='telegram'
            AND status='active' AND classification='matched'"""
        ).fetchone()[0]
        return {
            "matched": active_matched, "undetermined": active_unknown, "archived": archived,
            "pending_deletions": int(self.connection.execute("SELECT COUNT(*) FROM order_deletion_queue").fetchone()[0]),
            "source_total": int(source_row["total"]), "source_enabled": int(source_row["enabled"]),
            "source_errors": int(source_row["errors"]),
            "last_post": str(source_row["last_post"] or ""),
            "last_matched_order": str(latest_order or ""),
        }

    def archive_order(self, order_id: str, status: OrderStatus = OrderStatus.ARCHIVED) -> None:
        """Change archive state atomically, protecting every unresolved dispatch."""
        if status == OrderStatus.ACTIVE and self.connection.execute(
            "SELECT 1 FROM order_deletion_queue WHERE order_id=?", (order_id,)
        ).fetchone():
            raise ValueError('Сначала отмените ожидающее удаление заказа.')
        if status != OrderStatus.ACTIVE and self.connection.execute(
            """SELECT 1 FROM dispatch_jobs WHERE order_id=?
            AND status IN ('pending','preparing','awaiting_confirmation','running','sending','unknown_result')
            LIMIT 1""", (order_id,)
        ).fetchone():
            raise ValueError('Сначала разрешите выполняемую задачу или неизвестный результат отправки.')
        self.connection.execute("UPDATE orders SET status=?, archived_at=? WHERE id=?", (status.value, None if status == OrderStatus.ACTIVE else datetime.now(timezone.utc).isoformat(), order_id))
        self.connection.commit()

    def schedule_order_deletions(
        self, order_ids: list[str], requested_at: datetime, delete_after: datetime,
        batch_id: str, origin: str,
    ) -> dict[str, object]:
        """Queue only archived orders in one transaction and leave existing deadlines intact."""
        result: dict[str, object] = {
            "scheduled": 0, "already_pending": 0, "rejected_not_archived": 0,
            "not_found": 0, "scheduled_ids": [],
        }
        scheduled_ids: list[str] = []
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            for order_id in dict.fromkeys(str(value) for value in order_ids):
                row = self.connection.execute(
                    "SELECT status FROM orders WHERE id=?", (order_id,)
                ).fetchone()
                if row is None:
                    result["not_found"] += 1
                    continue
                if row["status"] != OrderStatus.ARCHIVED.value:
                    result["rejected_not_archived"] += 1
                    continue
                pending = self.connection.execute(
                    "SELECT 1 FROM order_deletion_queue WHERE order_id=?", (order_id,)
                ).fetchone()
                if pending is not None:
                    result["already_pending"] += 1
                    continue
                self.connection.execute(
                    """INSERT INTO order_deletion_queue
                    (order_id,requested_at,delete_after,batch_id,origin)
                    VALUES(?,?,?,?,?)""",
                    (order_id, self._format_datetime(requested_at), self._format_datetime(delete_after), batch_id, origin),
                )
                result["scheduled"] += 1
                scheduled_ids.append(order_id)
            result["scheduled_ids"] = scheduled_ids
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return result

    def list_order_deletions(self) -> list[dict[str, object]]:
        """Return durable deletion requests without joining or changing Order state."""
        rows = self.connection.execute(
            """SELECT order_id,requested_at,delete_after,batch_id,origin,last_error,
            last_attempt_at,attempt_count FROM order_deletion_queue ORDER BY delete_after,order_id"""
        ).fetchall()
        return [dict(row) for row in rows]

    def recover_order_deletion_deadlines(self, delete_after: datetime) -> int:
        """Give every pending request a fresh full protection window after restart."""
        cursor = self.connection.execute(
            "UPDATE order_deletion_queue SET delete_after=?",
            (self._format_datetime(delete_after),),
        )
        self.connection.commit()
        return cursor.rowcount

    def cancel_order_deletions(self, order_ids: list[str]) -> int:
        """Cancel queued deletions by stable Order ID while preserving archived Orders."""
        deleted = 0
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            for order_id in dict.fromkeys(str(value) for value in order_ids):
                cursor = self.connection.execute(
                    "DELETE FROM order_deletion_queue WHERE order_id=?", (order_id,)
                )
                deleted += cursor.rowcount
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return deleted

    def cancel_order_deletion_batch(self, batch_id: str) -> int:
        """Cancel the still-pending entries from one explicit bulk batch."""
        cursor = self.connection.execute(
            "DELETE FROM order_deletion_queue WHERE batch_id=?", (str(batch_id),)
        )
        self.connection.commit()
        return cursor.rowcount

    def record_order_deletion_attempt(self, order_id: str, error: str, attempted_at: datetime) -> None:
        """Persist a safe blocking/failure reason while retaining the deletion request."""
        self.connection.execute(
            """UPDATE order_deletion_queue SET last_error=?,last_attempt_at=?,attempt_count=attempt_count+1
            WHERE order_id=?""",
            (str(error)[:300], self._format_datetime(attempted_at), str(order_id)),
        )
        self.connection.commit()

    def hard_delete_archived_order(self, order_id: str, now: datetime) -> tuple[str, str]:
        """Atomically tombstone and remove one due archived Order and its owned rows."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """SELECT orders.id,orders.platform_id,orders.external_id,orders.status,
                queue.delete_after FROM orders JOIN order_deletion_queue AS queue ON queue.order_id=orders.id
                WHERE orders.id=?""",
                (str(order_id),),
            ).fetchone()
            if row is None:
                self.connection.rollback()
                return "not_found", "Заказ или запись очереди отсутствует."
            if row["status"] != OrderStatus.ARCHIVED.value:
                self.connection.rollback()
                return "blocked", "Заказ больше не находится в архиве."
            delete_after = self._parse_datetime(row["delete_after"])
            if delete_after is None or self._as_utc(delete_after) > self._as_utc(now):
                self.connection.rollback()
                return "not_due", "Срок удаления ещё не наступил."
            unresolved = self.connection.execute(
                """SELECT 1 FROM dispatch_jobs WHERE order_id=?
                AND status IN ('pending','preparing','awaiting_confirmation','running','sending','unknown_result','paused')
                LIMIT 1""",
                (str(order_id),),
            ).fetchone()
            if unresolved is not None:
                self.connection.rollback()
                return "blocked", "Есть незавершённая задача отправки."
            sending = self.connection.execute(
                "SELECT 1 FROM telegram_order_deliveries WHERE order_id=? AND status='sending' LIMIT 1",
                (str(order_id),),
            ).fetchone()
            if sending is not None:
                self.connection.rollback()
                return "blocked", "Идёт доставка Telegram-заказа."

            # Запоминаем исходное сообщение и репосты до удаления, чтобы история не импортировала их заново.
            identities = [(str(row["platform_id"]), str(row["external_id"]))]
            identities.extend(
                (str(alias["platform_id"]), str(alias["external_id"]))
                for alias in self.connection.execute(
                    "SELECT platform_id,external_id FROM telegram_message_aliases WHERE canonical_order_id=?",
                    (str(order_id),),
                ).fetchall()
            )
            deleted_at = self._format_datetime(now)
            for platform_id, external_id in dict.fromkeys(identities):
                self.connection.execute(
                    """INSERT INTO deleted_order_tombstones(platform_id,external_id,deleted_at)
                    VALUES(?,?,?) ON CONFLICT(platform_id,external_id) DO NOTHING""",
                    (platform_id, external_id, deleted_at),
                )
            self.connection.execute("DELETE FROM telegram_order_deliveries WHERE order_id=?", (str(order_id),))
            self.connection.execute("DELETE FROM dispatch_jobs WHERE order_id=?", (str(order_id),))
            self.connection.execute("DELETE FROM desktop_drafts WHERE order_id=?", (str(order_id),))
            self.connection.execute("DELETE FROM telegram_message_aliases WHERE canonical_order_id=?", (str(order_id),))
            self.connection.execute("DELETE FROM order_deletion_queue WHERE order_id=?", (str(order_id),))
            deleted = self.connection.execute("DELETE FROM orders WHERE id=?", (str(order_id),))
            if deleted.rowcount != 1:
                raise sqlite3.IntegrityError("Order disappeared during final deletion")
            self.connection.commit()
            return "deleted", ""
        except Exception:
            self.connection.rollback()
            raise

    def is_deleted_telegram_message(self, external_id: str) -> bool:
        """Check the compact manual-deletion tombstone used by both Telegram transports."""
        return self.connection.execute(
            "SELECT 1 FROM deleted_order_tombstones WHERE platform_id='telegram' AND external_id=?",
            (str(external_id),),
        ).fetchone() is not None

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        """Normalize legacy naive database times using the project's UTC convention."""
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def archive_expired_orders(self, now: datetime | None = None) -> int:
        """Archive active orders 12 hours after their original collection time."""
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        else:
            now = now.astimezone(timezone.utc)
        cutoff = now - timedelta(hours=12)
        rows = self.connection.execute(
            "SELECT id,discovered_at FROM orders WHERE status='active' AND discovered_at IS NOT NULL"
        ).fetchall()
        expired_ids = []
        for row in rows:
            try:
                collected_at = self._parse_datetime(row["discovered_at"])
            except (TypeError, ValueError):
                continue
            if collected_at is None:
                continue
            if collected_at.tzinfo is None:
                collected_at = collected_at.replace(tzinfo=timezone.utc)
            else:
                collected_at = collected_at.astimezone(timezone.utc)
            if collected_at <= cutoff:
                expired_ids.append(row["id"])
        if not expired_ids:
            return 0

        archived_at = now.isoformat()
        updated = 0
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            for offset in range(0, len(expired_ids), 500):
                batch = expired_ids[offset:offset + 500]
                placeholders = ",".join("?" for _ in batch)
                cursor = self.connection.execute(
                    f"""UPDATE orders SET status='archived',archived_at=?
                    WHERE status='active' AND id IN ({placeholders})""",
                    (archived_at, *batch),
                )
                updated += cursor.rowcount
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return updated

    def cleanup_archived_orders(self, now: datetime | None = None) -> int:
        """Delete expired archived content but retain a deduplication tombstone."""
        now = now or datetime.now(timezone.utc)
        rows = self.connection.execute(
            """SELECT orders.id, orders.platform_id, orders.external_id, orders.archived_at,
            (EXISTS(SELECT 1 FROM dispatch_jobs WHERE dispatch_jobs.order_id=orders.id)
             OR EXISTS(SELECT 1 FROM desktop_drafts WHERE desktop_drafts.order_id=orders.id AND text!='')) AS has_history
            FROM orders WHERE status IN ('archived', 'dismissed') AND archived_at IS NOT NULL
            AND NOT EXISTS(SELECT 1 FROM order_deletion_queue WHERE order_deletion_queue.order_id=orders.id)
            AND NOT EXISTS(SELECT 1 FROM dispatch_jobs WHERE dispatch_jobs.order_id=orders.id
                AND dispatch_jobs.status IN ('pending','preparing','running','sending','unknown_result'))"""
        ).fetchall()
        expired = []
        for row in rows:
            archived_at = self._parse_datetime(row["archived_at"])
            if archived_at is None:
                continue
            retention = timedelta(days=90 if row["has_history"] else 7)
            if now - archived_at >= retention:
                expired.append(row)
        for row in expired:
            self.connection.execute(
                """INSERT INTO order_tombstones(platform_id, external_id, archived_at, reason)
                VALUES(?,?,?,?) ON CONFLICT(platform_id, external_id) DO UPDATE SET archived_at=excluded.archived_at, reason=excluded.reason""",
                (row["platform_id"], row["external_id"], now.isoformat(), "archive_retention"),
            )
            self.connection.execute("DELETE FROM dispatch_jobs WHERE order_id=?", (row["id"],))
            self.connection.execute("DELETE FROM desktop_drafts WHERE order_id=?", (row["id"],))
            self.connection.execute("DELETE FROM orders WHERE id=?", (row["id"],))
        self.connection.commit()
        return len(expired)

    def save_account(self, account: Account, secret_ciphertext: str = "") -> None:
        self.connection.execute(
            """INSERT INTO accounts(id, platform_id, name, kind, enabled, requires_attention, secret_ciphertext)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET platform_id=excluded.platform_id,
            name=excluded.name, kind=excluded.kind, enabled=excluded.enabled,
            requires_attention=excluded.requires_attention""",
            (account.id, account.platform_id, account.name, account.kind.value, int(account.enabled),
             int(account.requires_attention), secret_ciphertext),
        )
        if secret_ciphertext:
            self.connection.execute("UPDATE accounts SET secret_ciphertext=? WHERE id=?", (secret_ciphertext, account.id))
        self.connection.commit()

    def load_accounts(self) -> list[Account]:
        rows = self.connection.execute("SELECT * FROM accounts ORDER BY name").fetchall()
        return [Account(row["id"], row["platform_id"], row["name"], AccountKind(row["kind"]),
                        bool(row["enabled"]), bool(row["requires_attention"])) for row in rows]

    def save_telegram_source(self, source: TelegramSource) -> None:
        source_id = source.source_id or f"telegram-{source.chat_id.replace('-', 'm').replace(':', 'x')}"
        self.connection.execute(
            """INSERT INTO telegram_sources(chat_id, name, enabled, language, poll_interval_seconds, pending_connection, reference, access_mode, access_status, source_id)
            VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET name=excluded.name,
            enabled=excluded.enabled, language=excluded.language,
            poll_interval_seconds=excluded.poll_interval_seconds, pending_connection=excluded.pending_connection,
            reference=excluded.reference, access_mode=excluded.access_mode, access_status=excluded.access_status,
            source_id=COALESCE(NULLIF(telegram_sources.source_id,''), excluded.source_id)""",
            (source.chat_id, source.name, int(source.enabled), source.language, source.poll_interval_seconds,
             int(source.pending_connection), source.reference, source.access_mode, source.access_status, source_id),
        )
        self.connection.commit()

    def delete_telegram_source(self, source_id: str) -> None:
        """Delete one source by stable source_id while retaining its orders."""
        row = self.connection.execute(
            "SELECT chat_id,source_id FROM telegram_sources WHERE source_id=? OR chat_id=? LIMIT 1", (source_id, source_id)
        ).fetchone()
        if row is None:
            return
        chat_id = str(row["chat_id"])
        self.connection.execute("DELETE FROM telegram_sources WHERE source_id=?", (str(row["source_id"]),))
        self.connection.execute("DELETE FROM telegram_source_state WHERE chat_id=?", (chat_id,))
        self.connection.execute("DELETE FROM telegram_inbox WHERE chat_id=? AND processed_at IS NULL", (chat_id,))
        self.connection.commit()

    def load_telegram_sources(self) -> list[TelegramSource]:
        rows = self.connection.execute("SELECT * FROM telegram_sources ORDER BY name, chat_id").fetchall()
        return [TelegramSource(
            row["chat_id"], row["name"], bool(row["enabled"]), row["language"], row["poll_interval_seconds"],
            bool(row["pending_connection"]), str(row["reference"] or ""), str(row["access_mode"] or "bot"),
            str(row["access_status"] or "available"), str(row["source_id"] or f"telegram-{row['chat_id']}"),
        ) for row in rows]

    def telegram_user_checkpoint(self, chat_id: str) -> dict[str, str]:
        """Return the durable User API progress for one configured source."""
        row = self.connection.execute(
            """SELECT user_last_message_id,history_synced_at,initial_history_from_at,
            initial_history_until_at,retry_after FROM telegram_source_state WHERE chat_id=?""", (chat_id,)
        ).fetchone()
        if row is None:
            return {"last_message_id": "", "history_synced_at": "", "initial_history_from_at": "",
                    "initial_history_until_at": "", "retry_after": ""}
        return {
            "last_message_id": str(row["user_last_message_id"] or ""),
            "history_synced_at": str(row["history_synced_at"] or ""),
            "initial_history_from_at": str(row["initial_history_from_at"] or ""),
            "initial_history_until_at": str(row["initial_history_until_at"] or ""),
            "retry_after": str(row["retry_after"] or ""),
        }

    def begin_telegram_user_initial_sync(self, chat_id: str, completed_at: datetime | None = None) -> None:
        """Persist the fixed one-hour bootstrap window for a newly verified User API source.

        The window is recorded before background work starts.  A retry therefore
        cannot silently expand it after a restart or a transient network error.
        """
        until = completed_at or datetime.now(timezone.utc)
        since = until - timedelta(hours=1)
        self.connection.execute(
            """INSERT INTO telegram_source_state(chat_id,initial_history_from_at,initial_history_until_at,last_error_code)
            VALUES(?,?,?,'') ON CONFLICT(chat_id) DO UPDATE SET
            initial_history_from_at=CASE WHEN telegram_source_state.history_synced_at IS NULL
            AND COALESCE(telegram_source_state.initial_history_from_at,'')='' THEN excluded.initial_history_from_at
            ELSE telegram_source_state.initial_history_from_at END,
            initial_history_until_at=CASE WHEN telegram_source_state.history_synced_at IS NULL
            AND COALESCE(telegram_source_state.initial_history_until_at,'')='' THEN excluded.initial_history_until_at
            ELSE telegram_source_state.initial_history_until_at END""",
            (chat_id, since.isoformat(), until.isoformat()),
        )
        self.connection.commit()

    def mark_telegram_user_message_saved(self, chat_id: str, message_id: str) -> None:
        """Advance User API progress only after the common order storage succeeds."""
        self.connection.execute(
            """INSERT INTO telegram_source_state(chat_id,user_last_message_id,last_error_code)
            VALUES(?,?,'') ON CONFLICT(chat_id) DO UPDATE SET
            user_last_message_id=CASE WHEN CAST(excluded.user_last_message_id AS INTEGER) >
            CAST(telegram_source_state.user_last_message_id AS INTEGER) THEN excluded.user_last_message_id
            ELSE telegram_source_state.user_last_message_id END, last_error_code=''""",
            (chat_id, str(message_id)),
        )
        self.connection.commit()

    def mark_telegram_user_history_synced(self, chat_id: str) -> None:
        """Record completion of the bounded initial User API history import."""
        self.connection.execute(
            """INSERT INTO telegram_source_state(chat_id,history_synced_at,last_error_code)
            VALUES(?,?, '') ON CONFLICT(chat_id) DO UPDATE SET
            history_synced_at=excluded.history_synced_at,last_error_code=''""",
            (chat_id, datetime.now(timezone.utc).isoformat()),
        )
        self.connection.commit()

    def record_telegram_user_wait(self, chat_id: str, retry_after: datetime, error_code: str = "flood_wait") -> None:
        """Persist a server-imposed wait without retaining Telegram message content."""
        self.connection.execute(
            """INSERT INTO telegram_source_state(chat_id,last_error_at,last_error_code,retry_after)
            VALUES(?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET
            last_error_at=excluded.last_error_at,last_error_code=excluded.last_error_code,retry_after=excluded.retry_after""",
            (chat_id, datetime.now(timezone.utc).isoformat(), error_code, retry_after.isoformat()),
        )
        self.connection.commit()

    def enqueue_telegram_update(self, update_id: int, payload: dict) -> bool:
        """Durably store one bot-wide update before acknowledging it to Telegram."""
        chat_id = str((payload.get("chat") or {}).get("id", ""))
        if not chat_id:
            raise ValueError("Telegram update does not contain a chat ID")
        cursor = self.connection.execute(
            """INSERT OR IGNORE INTO telegram_inbox(update_id, chat_id, payload, received_at)
            VALUES(?,?,?,?)""",
            (int(update_id), chat_id, json.dumps(payload, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def telegram_payload_for_message(self, chat_id: str, message_id: str) -> dict | None:
        """Return the durable source payload for an already received message, if retained."""
        rows = self.connection.execute(
            "SELECT payload FROM telegram_inbox WHERE chat_id=? ORDER BY update_id DESC", (str(chat_id),)
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                continue
            message = payload.get("message") or {}
            if str(message.get("message_id", "")) == str(message_id):
                return payload
        return None

    def pending_telegram_updates(self, chat_id: str) -> list[tuple[int, dict]]:
        """Return unprocessed updates for one source in Telegram delivery order."""
        rows = self.connection.execute(
            "SELECT update_id,payload FROM telegram_inbox WHERE chat_id=? AND processed_at IS NULL ORDER BY update_id",
            (chat_id,),
        ).fetchall()
        return [(int(row["update_id"]), json.loads(row["payload"])) for row in rows]

    def mark_telegram_update_processed(self, update_id: int) -> None:
        """Mark an already stored update complete after order persistence succeeds."""
        self.connection.execute(
            "UPDATE telegram_inbox SET processed_at=?, error_code='' WHERE update_id=?",
            (datetime.now(timezone.utc).isoformat(), int(update_id)),
        )
        self.connection.commit()

    def telegram_source_due(self, source: TelegramSource, now: datetime | None = None) -> bool:
        """Respect a source interval without delaying durable receipt of updates."""
        if not source.enabled:
            return False
        row = self.connection.execute(
            "SELECT last_processed_at FROM telegram_source_state WHERE chat_id=?", (source.chat_id,)
        ).fetchone()
        if row is None or not row["last_processed_at"]:
            return True
        previous = self._parse_datetime(row["last_processed_at"])
        return previous is None or (now or datetime.now(timezone.utc)) - previous >= timedelta(seconds=source.poll_interval_seconds)

    def mark_telegram_source_processed(self, chat_id: str, now: datetime | None = None) -> None:
        """Record a successful source batch and clear its transient error state."""
        self.connection.execute(
            """INSERT INTO telegram_source_state(chat_id,last_processed_at,last_error_at,last_error_code)
            VALUES(?,?,NULL,'') ON CONFLICT(chat_id) DO UPDATE SET
            last_processed_at=excluded.last_processed_at,last_error_at=NULL,last_error_code=''""",
            (chat_id, (now or datetime.now(timezone.utc)).isoformat()),
        )
        self.connection.commit()

    def record_telegram_source_error(self, chat_id: str, error_code: str) -> None:
        """Persist only a safe error code, never source message text or credentials."""
        now = datetime.now(timezone.utc).isoformat()
        self.connection.execute(
            """INSERT INTO telegram_source_state(chat_id,last_processed_at,last_error_at,last_error_code)
            VALUES(?,NULL,?,?) ON CONFLICT(chat_id) DO UPDATE SET
            last_error_at=excluded.last_error_at,last_error_code=excluded.last_error_code""",
            (chat_id, now, error_code),
        )
        self.connection.commit()

    def telegram_source_states(self) -> dict[str, dict[str, str]]:
        """Expose safe source processing status for the desktop settings view."""
        rows = self.connection.execute("SELECT * FROM telegram_source_state").fetchall()
        return {
            str(row["chat_id"]): {
                "last_processed_at": str(row["last_processed_at"] or ""),
                "last_error_code": str(row["last_error_code"] or ""),
                "user_last_message_id": str(row["user_last_message_id"] or ""),
                "history_synced_at": str(row["history_synced_at"] or ""),
                "initial_history_from_at": str(row["initial_history_from_at"] or ""),
                "initial_history_until_at": str(row["initial_history_until_at"] or ""),
                "retry_after": str(row["retry_after"] or ""),
            }
            for row in rows
        }

    def set_setting(self, key: str, value: str) -> None:
        self.connection.execute("INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self.connection.commit()

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def save_search_profile(self, profile: SearchProfile) -> None:
        """Persist all editable local filtering rules used for later intake."""
        value = json.dumps({
            "id": profile.id,
            "name": profile.name,
            "include_keywords": profile.include_keywords,
            "exclude_keywords": profile.exclude_keywords,
            "min_budget": profile.min_budget,
            "max_budget": profile.max_budget,
            "categories": profile.categories,
            "order_signals": profile.order_signals,
            "vacancy_signals": profile.vacancy_signals,
            "provider_ad_signals": profile.provider_ad_signals,
            "stop_phrases": profile.stop_phrases,
            "telegram_hard_stops": profile.telegram_hard_stops,
            "telegram_order_signals": profile.telegram_order_signals,
            "telegram_gate_configured": profile.telegram_gate_configured,
            "service_categories": [
                {"name": item.name, "enabled": item.enabled, "rules": item.rules,
                 "important_rules": item.important_rules}
                for item in profile.service_categories
            ],
        }, ensure_ascii=False)
        self.set_setting("search_profile", value)

    def load_search_profile(self) -> SearchProfile:
        """Load saved rules and upgrade old two-list profiles with local defaults."""
        try:
            value = json.loads(self.get_setting("search_profile", ""))
            if not isinstance(value, dict):
                raise ValueError
            from freelance_bot.core.filtering import default_search_profile
            from freelance_bot.core.models import ServiceCategory
            defaults = default_search_profile()
            categories = []
            has_saved_categories = "service_categories" in value
            raw_categories = value.get("service_categories", defaults.service_categories)
            for item in raw_categories:
                if isinstance(item, dict) and str(item.get("name") or "").strip():
                    categories.append(ServiceCategory(
                        str(item["name"]), bool(item.get("enabled", True)),
                        [str(rule) for rule in item.get("rules", []) if str(rule).strip()],
                        [str(rule) for rule in item.get("important_rules", []) if str(rule).strip()],
                    ))
            return SearchProfile(
                str(value.get("id") or "default"), str(value.get("name") or "Все заказы"),
                [str(item) for item in value.get("include_keywords", []) if str(item).strip()],
                [str(item) for item in value.get("exclude_keywords", []) if str(item).strip()],
                value.get("min_budget"), value.get("max_budget"),
                [str(item) for item in value.get("categories", []) if str(item).strip()],
                [str(item) for item in value.get("order_signals", defaults.order_signals) if str(item).strip()],
                [str(item) for item in value.get("vacancy_signals", defaults.vacancy_signals) if str(item).strip()],
                [str(item) for item in value.get("provider_ad_signals", defaults.provider_ad_signals) if str(item).strip()],
                [str(item) for item in value.get("stop_phrases", defaults.stop_phrases) if str(item).strip()],
                categories if has_saved_categories else defaults.service_categories,
                [str(item) for item in value.get("telegram_hard_stops", defaults.telegram_hard_stops) if str(item).strip()],
                [str(item) for item in value.get("telegram_order_signals", defaults.telegram_order_signals) if str(item).strip()],
                bool(value.get("telegram_gate_configured", True)),
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            from freelance_bot.core.filtering import default_search_profile
            return default_search_profile()

    def save_job(self, job: DispatchJob) -> None:
        self.connection.execute(
            """INSERT INTO dispatch_jobs(id, order_id, account_id, mode, status, proposal_text, attempts, next_attempt_at, error, confirmed_at, started_at, idempotency_key)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,
            proposal_text=excluded.proposal_text, attempts=excluded.attempts,
            next_attempt_at=excluded.next_attempt_at, error=excluded.error,
            confirmed_at=excluded.confirmed_at, started_at=excluded.started_at,
            idempotency_key=excluded.idempotency_key""",
            (job.id, job.order_id, job.account_id, job.mode.value, job.status.value, job.proposal_text,
             job.attempts, job.next_attempt_at.isoformat(), job.error,
             self._format_datetime(job.confirmed_at), self._format_datetime(job.started_at), job.idempotency_key),
        )
        self.connection.commit()

    def load_jobs(self) -> list[DispatchJob]:
        rows = self.connection.execute("SELECT * FROM dispatch_jobs WHERE status IN ('pending','preparing','awaiting_confirmation','running','sending','unknown_result')").fetchall()
        jobs = []
        for row in rows:
            # A task interrupted in an external action must never be retried blindly.
            status = JobStatus.UNKNOWN_RESULT if row["status"] in {JobStatus.RUNNING, JobStatus.SENDING} else JobStatus(row["status"])
            jobs.append(DispatchJob(row["id"], row["order_id"], row["account_id"], ProposalMode(row["mode"]),
                                    status, row["proposal_text"], row["attempts"],
                                    datetime.fromisoformat(row["next_attempt_at"]), row["error"],
                                    self._parse_datetime(row["confirmed_at"]), self._parse_datetime(row["started_at"]),
                                    row["idempotency_key"]))
        return jobs

    def desktop_state(self) -> tuple[dict, dict]:
        """Return persisted draft text and the latest dispatch state per order."""
        drafts = {row['order_id']: row['text'] for row in self.connection.execute('SELECT order_id,text FROM desktop_drafts')}
        jobs = {}
        for row in self.connection.execute('SELECT order_id,status,proposal_text FROM dispatch_jobs ORDER BY rowid'):
            jobs[row['order_id']] = dict(row)
        return drafts, jobs

    def save_desktop_draft(self, order_id: str, text: str) -> None:
        """Persist editable text without confirming or enqueueing an external send."""
        if self.get_order(order_id) is None:
            raise ValueError('Заказ больше не существует.')
        self.connection.execute('INSERT INTO desktop_drafts(order_id,text,updated_at) VALUES(?,?,?) ON CONFLICT(order_id) DO UPDATE SET text=excluded.text,updated_at=excluded.updated_at', (order_id, text, datetime.now(timezone.utc).isoformat()))
        self.connection.commit()

    @staticmethod
    def _format_datetime(value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    @staticmethod
    def _parse_datetime(value: str | None) -> datetime | None:
        return datetime.fromisoformat(value) if value else None

    @staticmethod
    def _row_to_order(row: sqlite3.Row) -> Order:
        return Order(
            row["id"], row["platform_id"], row["external_id"], row["title"], row["description"], row["url"], row["budget"],
            currency=row["currency"], metadata=json.loads(row["payload"]), status=OrderStatus(row["status"]),
            classification=OrderClassification(row["classification"]),
            content_fingerprint=str(row["content_fingerprint"] or ""),
            published_at=Database._parse_datetime(row["published_at"]),
            discovered_at=Database._parse_datetime(row["discovered_at"]),
            last_checked_at=Database._parse_datetime(row["last_checked_at"]),
            archived_at=Database._parse_datetime(row["archived_at"]),
        )

    def count(self, table: str) -> int:
        """Return a count for known local tables used by the dashboard."""
        if table not in {"accounts", "orders", "dispatch_jobs", "action_logs"}:
            raise ValueError("Unknown table")
        return int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def log(self, account_id: str, action: str, status: str, details: str = "") -> None:
        self.connection.execute("INSERT INTO action_logs(account_id, action, status, details) VALUES(?,?,?,?)", (account_id, action, status, details))
        self.connection.commit()

    def list_logs(self, limit: int = 100) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT account_id, action, status, details, created_at FROM action_logs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def close(self) -> None:
        self.connection.close()
