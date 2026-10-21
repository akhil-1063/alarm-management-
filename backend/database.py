"""SQLAlchemy engine / session setup."""
from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from backend.config import settings

connect_args = {}
if settings.database_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(settings.database_url, connect_args=connect_args, future=True)

if settings.database_url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _ensure_sqlite_columns() -> None:
    """Non-destructive column migration for existing SQLite databases.

    create_all() does not alter existing tables. When new columns are added to
    the models, this helper appends them (and nothing else) so the development
    database keeps every historical record.
    """
    from sqlalchemy import inspect, text

    from backend import models  # noqa: F401

    inspector = inspect(engine)
    wanted: dict[str, list[tuple[str, str]]] = {
        "devices": [
            ("last_ingest_at", "DATETIME"),
            ("last_ingest_status", "VARCHAR(32)"),
            ("last_error", "TEXT"),
            ("data_endpoint", "VARCHAR(128)"),
            ("direct_fetch_enabled", "BOOLEAN DEFAULT 0"),
        ],
        "sensor_readings": [
            ("systolic_bp", "FLOAT"),
            ("diastolic_bp", "FLOAT"),
            ("bp_unit", "VARCHAR(16) DEFAULT 'mmHg'"),
            ("bp_simulated", "BOOLEAN DEFAULT 0"),
            ("bp_pot_raw", "INTEGER"),
            ("finger_present", "BOOLEAN DEFAULT 1"),
            ("hr_valid", "BOOLEAN DEFAULT 0"),
            ("spo2_valid", "BOOLEAN DEFAULT 0"),
            ("buzzer_alert", "BOOLEAN DEFAULT 0"),
        ],
    }
    with engine.begin() as conn:
        for table, cols in wanted.items():
            if table not in inspector.get_table_names():
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            for name, ddl_type in cols:
                if name not in existing:
                    conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl_type}'))


def init_db() -> None:
    from backend import models  # noqa: F401  (register models)

    Base.metadata.create_all(bind=engine)
    _ensure_sqlite_columns()
