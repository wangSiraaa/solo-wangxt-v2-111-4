"""测试夹具：

- 若配置的 PostgreSQL（默认 127.0.0.1:55432/rawmix）可连接：沿用原 PG 口径，
  不做任何注入，原 test_api.py 行为完全不变（“既有化验版与历史运行记录保持不变”）。
- 否则回退到临时 SQLite（SQLAlchemy 方言无关），播种同一份虚构演示数据，
  供无 PG 的环境运行完整 HTTP 行为测试；并令依赖 PG 的 test_api.py 自动跳过。
"""
import os

import pytest

# 必须在导入任何 app.* 之前决定数据库。默认 PG 不可达时回退临时 SQLite。
_DEFAULT_PG = "postgresql+psycopg2://mixapp@127.0.0.1:55432/rawmix"
_cfg_url = os.environ.get("RAWMIX_DATABASE_URL", _DEFAULT_PG)


def _pg_alive(url: str) -> bool:
    if not url.startswith(("postgresql", "postgres")):
        return False
    try:
        from sqlalchemy import create_engine, text
        eng = create_engine(url, connect_args={"connect_timeout": 2})
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


PG_AVAILABLE = _pg_alive(_cfg_url)

if not PG_AVAILABLE:
    _TMP_DB = "/tmp/rawmix_test_api.db"
    if os.path.exists(_TMP_DB):
        os.remove(_TMP_DB)
    os.environ["RAWMIX_DATABASE_URL"] = f"sqlite:///{_TMP_DB}"

    def pytest_collection_modifyitems(session, config, items):
        skip_pg = pytest.mark.skip(reason="PostgreSQL 不可用，该用例由 test_robust_api.py 覆盖")
        for it in items:
            if os.path.basename(it.fspath) == "test_api.py":
                it.add_marker(skip_pg)


@pytest.fixture(scope="session", autouse=True)
def _seeded_db():
    if PG_AVAILABLE:
        # PG 环境沿用脚本播种，不自动写库；用例假定 seed.sh 已执行
        yield
        return
    from app import seed as seed_mod
    import copy
    from app import models
    from app.database import SessionLocal, ensure_schema
    ensure_schema()
    db = SessionLocal()
    try:
        if not db.query(models.Material).first():
            for spec0 in seed_mod.MATERIALS:
                spec = copy.deepcopy(spec0)
                versions = spec.pop("versions")
                mat = models.Material(**spec)
                db.add(mat)
                db.flush()
                for v in versions:
                    db.add(models.AssayVersion(material_id=mat.id, **v))
            db.commit()
    finally:
        db.close()
    yield


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)
