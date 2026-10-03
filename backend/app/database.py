from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import DATABASE_URL

engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)
Base = declarative_base()

# 检测不确定度功能新增列（无独立迁移工具；幂等补列，不改既有数据）。
_ADDED_COLUMNS = {
    "assay_version": [("uncertainties", "JSON")],
    "blend_run": [("uncertainty_snapshot", "JSON")],
    "blend_solution": [
        ("robust", "BOOLEAN DEFAULT FALSE"),
        ("worst_case", "JSON"),
    ],
    "blend_item": [("uncertainty_snapshot", "JSON")],
}


def ensure_new_columns() -> None:
    """对既有数据库幂等补列；列已存在时跳过，旧化验版/旧运行保持原样。"""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table, cols in _ADDED_COLUMNS.items():
            if table not in existing_tables:
                continue  # create_all 会按最新模型建表
            present = {c["name"] for c in insp.get_columns(table)}
            for name, ddl_type in cols:
                if name in present:
                    continue
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
