"""
M1: Database Configuration
Owner: Backend Dev 1
Dependencies: None

SQLAlchemy async setup with SQLite (swap to PostgreSQL for production).
"""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

_is_postgres = settings.DATABASE_URL.startswith("postgresql")

if _is_postgres:
    # An explicit ?ssl= in the URL wins (local Postgres usually has no SSL);
    # otherwise require SSL (managed/prod databases).
    _connect_args = {} if "ssl=" in settings.DATABASE_URL else {"ssl": "require"}
    _pool_kwargs = {"pool_size": 10, "max_overflow": 20}
else:
    # SQLite on EFS: single-writer safe settings.
    # - timeout=30: retry busy-locked DB for up to 30s before raising
    # - check_same_thread=False: required for async (aiosqlite uses threads)
    # - journal_mode=DELETE (default): safe on NFS/EFS; WAL uses shm which
    #   breaks on network filesystems
    _connect_args = {"timeout": 30, "check_same_thread": False}
    _pool_kwargs = {}

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    connect_args=_connect_args,
    **_pool_kwargs,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency injection for FastAPI routes."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def _run_migration(conn, sql: str) -> None:
    """Run a migration statement in a savepoint so a failure never aborts the outer transaction."""
    _text = __import__("sqlalchemy").text
    try:
        await conn.execute(_text("SAVEPOINT _mig"))
        await conn.execute(_text(sql))
        await conn.execute(_text("RELEASE SAVEPOINT _mig"))
    except Exception:
        try:
            await conn.execute(_text("ROLLBACK TO SAVEPOINT _mig"))
        except Exception:
            pass


# Keep old name as alias so nothing else breaks.
_add_column_if_missing = _run_migration


# ─── Data migrations (both dialects) ──────────────────────────────────────────
#
# The DDL above is idempotent; the transformations below are one-shot data
# corrections. Applied versions are recorded in the _data_migrations marker
# table so re-boots never double-apply (Production Data & Compatibility
# Policy in AGENTS.md: stored data must stay usable when a value's meaning
# changes — migrate it, never strand it).

_APPOINTMENT_TZ_VERSION = "002_appointment_tz_normalization"
_NULL_CHAT_SESSIONS_VERSION = "001_drop_null_campaign_chat_sessions"

# Appointments booked through SurveyPage before timezone-aware booking stored
# true UTC instants (the page sent toISOString() and the backend stripped the
# Z). Writes after this cutoff are already campaign-local wall time, because
# book_appointment converts aware input via to_naive_local(). Set to the first
# production deploy date of the tz-aware booking code.
_LEGACY_TZ_CUTOFF = datetime(2026, 8, 24)


async def _run_data_migrations(conn) -> None:
    """Best-effort boot-time data migrations, marker-guarded against re-runs.

    Runs inside init_db's open transaction: either everything commits or a
    failed migration rolls back and simply retries on next boot.
    """
    import logging

    logger = logging.getLogger(__name__)

    from sqlalchemy import select, text

    from app.core.timeutils import get_zone
    from app.models.models import Advertisement
    from app.models.survey import Appointment

    try:
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS _data_migrations "
                "(version VARCHAR(64) PRIMARY KEY, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
            )
        )
    except Exception as exc:
        logger.warning("Could not create _data_migrations marker table — skipping data migrations: %s", exc)
        return

    # ── SMS tenant backfill — idempotent by NULL guard, no marker needed ──────
    # Legacy rows predate company_id stamping and would be invisible to
    # non-super users without this.
    await conn.execute(
        text(
            "UPDATE sms_campaigns SET company_id = "
            "(SELECT company_id FROM advertisements WHERE id = advertisement_id) "
            "WHERE company_id IS NULL"
        )
    )
    await conn.execute(
        text(
            "UPDATE sms_messages SET company_id = "
            "(SELECT adv.company_id FROM appointments ap "
            "JOIN advertisements adv ON adv.id = ap.advertisement_id "
            "WHERE ap.id = sms_messages.appointment_id) "
            "WHERE company_id IS NULL"
        )
    )

    # ── Legacy null-campaign chat sessions (one-shot, DEBUG only) ───────────
    # These rows predate the campaign_id column. Prod behavior is unchanged
    # (never deleted); dev boots used to wipe them on every restart.
    if settings.DEBUG:
        _done = await conn.execute(
            text("SELECT version FROM _data_migrations WHERE version = :v"),
            {"v": _NULL_CHAT_SESSIONS_VERSION},
        )
        if _done.first() is None:
            await conn.execute(text("DELETE FROM chat_sessions WHERE campaign_id IS NULL"))
            await conn.execute(
                text("INSERT INTO _data_migrations (version) VALUES (:v)"),
                {"v": _NULL_CHAT_SESSIONS_VERSION},
            )

    # ── Appointment timezone normalization (one-shot) ──────────────────────────
    already = await conn.execute(
        text("SELECT version FROM _data_migrations WHERE version = :v"),
        {"v": _APPOINTMENT_TZ_VERSION},
    )
    if already.first() is not None:
        return

    rows = (
        await conn.execute(
            select(Appointment.id, Appointment.slot_datetime, Advertisement.bot_config)
            .join(Advertisement, Advertisement.id == Appointment.advertisement_id)
            .where(
                # Only survey-attributed bookings were written as UTC instants;
                # landing-page / chat / voice channels wrote naive wall times
                # that must NOT be shifted.
                Appointment.survey_response_id.isnot(None),
                Appointment.created_at < _LEGACY_TZ_CUTOFF,
            )
        )
    ).all()

    updates = []
    for ad_id_, slot_dt, bot_config in rows:
        if slot_dt is None:
            continue
        # Detection heuristic: a UTC instant of a Sydney business-hours booking
        # lands outside 05–21 h when its hour is read raw. Rows that were
        # already wall time keep business-hours values and are protected by
        # this filter. Residual: an evening survey booking stored as UTC
        # (raw hour 05–21) stays drifted — acceptable and documented in
        # docs/DEPLOYMENT.md.
        if 5 <= slot_dt.hour <= 21:
            continue
        cfg = bot_config if isinstance(bot_config, dict) else {}
        zone = get_zone(cfg.get("timezone"))
        # Interpret the stored naive value as a UTC instant, render it in the
        # campaign's timezone, strip tzinfo → campaign-local wall time.
        local_dt = slot_dt.replace(tzinfo=UTC).astimezone(zone).replace(tzinfo=None)
        updates.append({"aid": ad_id_, "dt": local_dt})

    for u in updates:
        await conn.execute(text("UPDATE appointments SET slot_datetime = :dt WHERE id = :aid"), u)

    await conn.execute(text("INSERT INTO _data_migrations (version) VALUES (:v)"), {"v": _APPOINTMENT_TZ_VERSION})
    if updates:
        logger.warning(
            "Data migration %s: converted %d of %d legacy survey appointments from UTC instants to campaign-local",
            _APPOINTMENT_TZ_VERSION,
            len(updates),
            len(rows),
        )


async def init_db():
    """Create all tables and apply lightweight column migrations.

    Explicit model import ensures Base.metadata is fully populated
    regardless of which routes happen to be loaded first.

    create_all handles fresh databases for both SQLite and PostgreSQL.
    The ALTER TABLE migrations below only apply when upgrading an existing
    PostgreSQL database that predates certain column additions.
    """
    import app.models.models  # noqa: F401 — registers all ORM classes on Base.metadata

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, checkfirst=True)

        if not _is_postgres:
            # SQLite: create_all creates new tables but does NOT alter existing ones.
            # Add missing columns via try/except (SQLite has no IF NOT EXISTS for ALTER TABLE).
            _sql = __import__("sqlalchemy").text

            _sqlite_cols = [
                "ALTER TABLE chat_sessions ADD COLUMN updated_at DATETIME;",
                "ALTER TABLE advertisement_documents ADD COLUMN content TEXT;",
                "ALTER TABLE company_documents ADD COLUMN priority INTEGER DEFAULT 0;",
                "ALTER TABLE company_documents ADD COLUMN version INTEGER DEFAULT 1;",
                "ALTER TABLE advertisements ADD COLUMN campaign_category VARCHAR(64);",
                "ALTER TABLE advertisements ADD COLUMN special_instructions TEXT;",
                "ALTER TABLE advertisements ADD COLUMN questionnaire JSON;",
                "ALTER TABLE advertisements ADD COLUMN duration VARCHAR(128);",
                "ALTER TABLE advertisements ADD COLUMN trial_location JSON;",
                "ALTER TABLE advertisements ADD COLUMN patients_required INTEGER;",
                "ALTER TABLE advertisements ADD COLUMN trial_start_date DATE;",
                "ALTER TABLE advertisements ADD COLUMN trial_end_date DATE;",
                "ALTER TABLE advertisements ADD COLUMN hosted_url VARCHAR(1024);",
                "ALTER TABLE companies ADD COLUMN locations JSON;",
                # platform_connections: new table handled by create_all; these cover
                # any columns added after the initial table creation on existing DBs.
                "ALTER TABLE platform_connections ADD COLUMN meta_user_id VARCHAR(128);",
                "ALTER TABLE platform_connections ADD COLUMN ad_account_name VARCHAR(256);",
                "ALTER TABLE platform_connections ADD COLUMN page_name VARCHAR(256);",
                # ad_analytics: Meta-sourced insight columns
                "ALTER TABLE ad_analytics ADD COLUMN spend REAL;",
                "ALTER TABLE ad_analytics ADD COLUMN reach INTEGER;",
                "ALTER TABLE ad_analytics ADD COLUMN cpm REAL;",
                "ALTER TABLE ad_analytics ADD COLUMN date_label VARCHAR(32);",
                "ALTER TABLE ad_analytics ADD COLUMN source VARCHAR(16) DEFAULT 'local';",
                "ALTER TABLE optimizer_logs ADD COLUMN status VARCHAR(16) DEFAULT 'done';",
                "ALTER TABLE optimizer_logs ADD COLUMN context JSON;",
                "ALTER TABLE optimizer_logs ADD COLUMN human_decision VARCHAR(32);",
                "ALTER TABLE optimizer_logs ADD COLUMN applied_changes JSON;",
                # Voice session outbound call tracking
                "ALTER TABLE voice_sessions ADD COLUMN phone VARCHAR(32);",
                "ALTER TABLE voice_sessions ADD COLUMN survey_response_id VARCHAR;",
                # Conversation analysis results
                "ALTER TABLE voice_sessions ADD COLUMN call_analysis JSON;",
                "ALTER TABLE chat_sessions ADD COLUMN chat_analysis JSON;",
                "ALTER TABLE advertisements ADD COLUMN booking_config JSON;",
                "ALTER TABLE advertisements ADD COLUMN shorts_files JSON;",
                # Appointment booking source tracking
                "ALTER TABLE appointments ADD COLUMN voice_session_id VARCHAR;",
                "ALTER TABLE appointments ADD COLUMN chat_session_id VARCHAR;",
                # SMS tenant scoping
                "ALTER TABLE sms_messages ADD COLUMN company_id VARCHAR;",
                "ALTER TABLE sms_campaigns ADD COLUMN company_id VARCHAR;",
                # AI generation failure surfacing
                "ALTER TABLE advertisements ADD COLUMN generation_error TEXT;",
                # JWT revocation counter
                "ALTER TABLE users ADD COLUMN token_version INTEGER DEFAULT 0;",
            ]
            for stmt in _sqlite_cols:
                try:
                    await conn.execute(_sql(stmt))
                except Exception as exc:
                    if "duplicate column name" in str(exc).lower():
                        continue  # column already exists — expected on warm DBs
                    import logging

                    logging.getLogger(__name__).warning("Migration statement failed (continuing): %s (%s)", stmt, exc)

            # Unique tenant name — best-effort: fails harmlessly if legacy duplicates exist.
            try:
                await conn.execute(_sql("CREATE UNIQUE INDEX IF NOT EXISTS ux_companies_name ON companies (name);"))
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "Could not create unique index on companies.name (duplicates present?): %s", exc
                )

            # Normalise role column to enum member NAMES (what SQLAlchemy stores).
            await conn.execute(
                _sql("UPDATE users SET role = 'STUDY_COORDINATOR' WHERE role IN ('ADMIN', 'study_coordinator');")
            )
            await conn.execute(
                _sql("UPDATE users SET role = 'PROJECT_MANAGER'   WHERE role IN ('REVIEWER', 'project_manager');")
            )
            await conn.execute(
                _sql("UPDATE users SET role = 'ETHICS_MANAGER'    WHERE role IN ('ETHICS_REVIEWER', 'ethics_manager');")
            )
            await conn.execute(_sql("UPDATE users SET role = 'PUBLISHER'         WHERE role = 'publisher';"))

            # Mark as onboarded any company that has at least one user but
            # onboarded=0 — these were locked out due to training failures.
            await conn.execute(
                _sql(
                    "UPDATE companies SET onboarded = 1 WHERE onboarded = 0 "
                    "AND id IN (SELECT DISTINCT company_id FROM users);"
                )
            )
            await _run_data_migrations(conn)
            return

        # ── PostgreSQL upgrade migrations ─────────────────────────────────────
        _sql = __import__("sqlalchemy").text

        # Unique tenant name — best-effort: fails harmlessly if legacy duplicates exist.
        try:
            await conn.execute(_sql("CREATE UNIQUE INDEX IF NOT EXISTS ux_companies_name ON companies (name);"))
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning(
                "Could not create unique index on companies.name (duplicates present?): %s", exc
            )

        await _add_column_if_missing(conn, "ALTER TABLE advertisement_documents ADD COLUMN IF NOT EXISTS content TEXT;")
        await _add_column_if_missing(
            conn, "ALTER TABLE company_documents ADD COLUMN IF NOT EXISTS priority INTEGER DEFAULT 0;"
        )
        await _add_column_if_missing(
            conn, "ALTER TABLE company_documents ADD COLUMN IF NOT EXISTS version INTEGER DEFAULT 1;"
        )
        await _add_column_if_missing(
            conn, "ALTER TABLE advertisements ADD COLUMN IF NOT EXISTS campaign_category VARCHAR(64);"
        )
        await _add_column_if_missing(
            conn, "ALTER TABLE advertisements ADD COLUMN IF NOT EXISTS special_instructions TEXT;"
        )
        await _add_column_if_missing(conn, "ALTER TABLE advertisements ADD COLUMN IF NOT EXISTS questionnaire JSON;")
        await _add_column_if_missing(conn, "ALTER TABLE advertisements ADD COLUMN IF NOT EXISTS duration VARCHAR(128);")
        await _add_column_if_missing(conn, "ALTER TABLE companies ADD COLUMN IF NOT EXISTS locations JSON;")
        await _add_column_if_missing(conn, "ALTER TABLE advertisements ADD COLUMN IF NOT EXISTS trial_location JSON;")
        await _add_column_if_missing(
            conn, "ALTER TABLE advertisements ADD COLUMN IF NOT EXISTS patients_required INTEGER;"
        )
        await _add_column_if_missing(conn, "ALTER TABLE advertisements ADD COLUMN IF NOT EXISTS trial_start_date DATE;")
        await _add_column_if_missing(conn, "ALTER TABLE advertisements ADD COLUMN IF NOT EXISTS trial_end_date DATE;")
        await _add_column_if_missing(
            conn, "ALTER TABLE advertisements ADD COLUMN IF NOT EXISTS hosted_url VARCHAR(1024);"
        )
        await _add_column_if_missing(
            conn, "ALTER TABLE platform_connections ADD COLUMN IF NOT EXISTS meta_user_id VARCHAR(128);"
        )
        await _add_column_if_missing(
            conn, "ALTER TABLE platform_connections ADD COLUMN IF NOT EXISTS ad_account_name VARCHAR(256);"
        )
        await _add_column_if_missing(
            conn, "ALTER TABLE platform_connections ADD COLUMN IF NOT EXISTS page_name VARCHAR(256);"
        )
        await _add_column_if_missing(conn, "ALTER TABLE ad_analytics ADD COLUMN IF NOT EXISTS spend FLOAT;")
        await _add_column_if_missing(conn, "ALTER TABLE ad_analytics ADD COLUMN IF NOT EXISTS reach INTEGER;")
        await _add_column_if_missing(conn, "ALTER TABLE ad_analytics ADD COLUMN IF NOT EXISTS cpm FLOAT;")
        await _add_column_if_missing(conn, "ALTER TABLE ad_analytics ADD COLUMN IF NOT EXISTS date_label VARCHAR(32);")
        await _add_column_if_missing(
            conn, "ALTER TABLE ad_analytics ADD COLUMN IF NOT EXISTS source VARCHAR(16) DEFAULT 'local';"
        )
        await _add_column_if_missing(
            conn, "ALTER TABLE optimizer_logs ADD COLUMN IF NOT EXISTS status VARCHAR(16) DEFAULT 'done';"
        )
        await _add_column_if_missing(conn, "ALTER TABLE optimizer_logs ADD COLUMN IF NOT EXISTS context JSON;")
        await _add_column_if_missing(
            conn, "ALTER TABLE optimizer_logs ADD COLUMN IF NOT EXISTS human_decision VARCHAR(32);"
        )
        await _add_column_if_missing(conn, "ALTER TABLE optimizer_logs ADD COLUMN IF NOT EXISTS applied_changes JSON;")
        await _add_column_if_missing(conn, "ALTER TABLE voice_sessions ADD COLUMN IF NOT EXISTS phone VARCHAR(32);")
        await _add_column_if_missing(
            conn, "ALTER TABLE voice_sessions ADD COLUMN IF NOT EXISTS survey_response_id VARCHAR;"
        )
        await _add_column_if_missing(conn, "ALTER TABLE voice_sessions ADD COLUMN IF NOT EXISTS call_analysis JSON;")
        await _add_column_if_missing(conn, "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS chat_analysis JSON;")
        await _add_column_if_missing(conn, "ALTER TABLE advertisements ADD COLUMN IF NOT EXISTS booking_config JSON;")
        await _add_column_if_missing(conn, "ALTER TABLE advertisements ADD COLUMN IF NOT EXISTS generation_error TEXT;")
        await _add_column_if_missing(
            conn, "ALTER TABLE appointments ADD COLUMN IF NOT EXISTS voice_session_id VARCHAR;"
        )
        # SMS tenant scoping
        await _add_column_if_missing(conn, "ALTER TABLE sms_messages ADD COLUMN IF NOT EXISTS company_id VARCHAR;")
        await _add_column_if_missing(conn, "ALTER TABLE sms_campaigns ADD COLUMN IF NOT EXISTS company_id VARCHAR;")
        await _add_column_if_missing(
            conn, "ALTER TABLE users ADD COLUMN IF NOT EXISTS token_version INTEGER DEFAULT 0;"
        )

        # chat_sessions.campaign_id — added after initial table creation.
        await _run_migration(conn, "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS campaign_id VARCHAR;")
        # Add FK constraint separately (IF NOT EXISTS on constraints needs PG15+, use DO block).
        await _run_migration(
            conn,
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conrelid = 'chat_sessions'::regclass
                      AND conname = 'chat_sessions_campaign_id_fkey'
                ) THEN
                    ALTER TABLE chat_sessions
                        ADD CONSTRAINT chat_sessions_campaign_id_fkey
                        FOREIGN KEY (campaign_id) REFERENCES advertisements(id) ON DELETE CASCADE;
                END IF;
            END $$;
        """,
        )
        # (Null-campaign session cleanup lives in _run_data_migrations,
        # marker-guarded so it runs at most once per environment.)

        # Recreate platform_connections FKs with ON DELETE CASCADE so that
        # deleting a company or user automatically removes their connections.
        await _run_migration(
            conn,
            """
            DO $$
            DECLARE _cn text;
            BEGIN
                SELECT conname INTO _cn FROM pg_constraint
                WHERE conrelid = 'platform_connections'::regclass AND contype = 'f'
                  AND confrelid = 'companies'::regclass;
                IF _cn IS NOT NULL AND _cn != 'platform_connections_company_id_fkey' THEN
                    EXECUTE 'ALTER TABLE platform_connections DROP CONSTRAINT ' || quote_ident(_cn);
                END IF;
            END $$;
        """,
        )
        await _run_migration(
            conn,
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conrelid = 'platform_connections'::regclass
                      AND conname = 'platform_connections_company_id_fkey'
                ) THEN
                    ALTER TABLE platform_connections
                        ADD CONSTRAINT platform_connections_company_id_fkey
                        FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE;
                END IF;
            END $$;
        """,
        )
        await _run_migration(
            conn,
            """
            DO $$
            DECLARE _cn text;
            BEGIN
                SELECT conname INTO _cn FROM pg_constraint
                WHERE conrelid = 'platform_connections'::regclass AND contype = 'f'
                  AND confrelid = 'users'::regclass;
                IF _cn IS NOT NULL AND _cn != 'platform_connections_user_id_fkey' THEN
                    EXECUTE 'ALTER TABLE platform_connections DROP CONSTRAINT ' || quote_ident(_cn);
                END IF;
            END $$;
        """,
        )
        await _run_migration(
            conn,
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conrelid = 'platform_connections'::regclass
                      AND conname = 'platform_connections_user_id_fkey'
                ) THEN
                    ALTER TABLE platform_connections
                        ADD CONSTRAINT platform_connections_user_id_fkey
                        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
                END IF;
            END $$;
        """,
        )

        # chat_sessions unique index
        await _run_migration(
            conn, "CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_session ON chat_sessions (campaign_id, session_id);"
        )

        # Deduplicate skill_configs before adding unique constraint.
        await _run_migration(
            conn,
            """
            DELETE FROM skill_configs
            WHERE id NOT IN (
                SELECT DISTINCT ON (company_id, skill_type) id
                FROM skill_configs
                ORDER BY company_id, skill_type, version DESC, updated_at DESC
            );
        """,
        )

        # Unique constraint for ON CONFLICT upsert in trainer.py
        await _run_migration(
            conn,
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_skill_configs_company_skill "
            "ON skill_configs (company_id, skill_type);",
        )

        # Migrate userrole enum values (PostgreSQL enums only).
        # ALTER TYPE ... ADD VALUE cannot run inside a transaction block — use AUTOCOMMIT.
        async with engine.connect() as _ac:
            await _ac.execution_options(isolation_level="AUTOCOMMIT")
            for new_role in ("STUDY_COORDINATOR", "PROJECT_MANAGER", "ETHICS_MANAGER", "PUBLISHER"):
                try:
                    await _ac.execute(_sql(f"ALTER TYPE userrole ADD VALUE IF NOT EXISTS '{new_role}';"))
                except Exception:
                    pass  # value already exists
            # Add GENERATING to adstatus enum if it doesn't exist yet
            try:
                await _ac.execute(_sql("ALTER TYPE adstatus ADD VALUE IF NOT EXISTS 'generating';"))
            except Exception:
                pass

        # Normalise role column to enum member NAMES (what SQLAlchemy stores).
        # Cast to text so legacy string literals that aren't valid enum values
        # don't raise "invalid input value for enum" on a fresh database.
        await _run_migration(
            conn, "UPDATE users SET role = 'STUDY_COORDINATOR' WHERE role::text IN ('ADMIN', 'study_coordinator');"
        )
        await _run_migration(
            conn, "UPDATE users SET role = 'PROJECT_MANAGER'   WHERE role::text IN ('REVIEWER', 'project_manager');"
        )
        await _run_migration(
            conn, "UPDATE users SET role = 'ETHICS_MANAGER'    WHERE role::text IN ('ETHICS_REVIEWER', 'ethics_manager');"
        )
        await _run_migration(conn, "UPDATE users SET role = 'PUBLISHER' WHERE role::text = 'publisher';")

        await _run_data_migrations(conn)
