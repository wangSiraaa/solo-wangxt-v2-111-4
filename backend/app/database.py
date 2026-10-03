from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import DATABASE_URL

engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)
Base = declarative_base()


# 既有库的幂等加列（新列全部可空，旧行保持 NULL/历史快照不被改写）。
_ADDED_COLUMNS = {
    "assay_version": [
        ("uncertainty", "JSON"),
    ],
    "blend_solution": [
        ("robust_mode", "VARCHAR(16)"),
        ("robust_report", "JSON"),
    ],
    "blend_item": [
        ("uncertainty_trace", "JSON"),
        ("worst_case_snapshot", "JSON"),
    ],
}


def ensure_schema() -> None:
    """create_all 建新表；对已存在的旧表幂等 ALTER ADD COLUMN（可空）。"""
    Base.metadata.create_all(bind=engine)
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table, cols in _ADDED_COLUMNS.items():
            if table not in existing_tables:
                continue
            have = {c["name"] for c in insp.get_columns(table)}
            for name, ddl_type in cols:
                if name not in have:
                    conn.execute(text(
                        f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}"
                    ))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
