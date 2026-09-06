import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Deterministic test environment; set before app.config is imported anywhere.
os.environ["ENCRYPTION_KEY"] = "fZk5x4nq2r7YpE8Lw1vS0tUaMbHcDgJiKlOoNPQRSTU="
os.environ["LLM_MODE"] = "mock"
os.environ["MAIL_WEBHOOK_SECRET"] = "test-secret"
os.environ["APPLY_DOMAIN"] = "apply.test"
os.environ["MAIL_PROVIDER"] = "log"

import pytest  # noqa: E402

from app.config import get_settings, reset_settings_cache  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _settings(tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    os.environ["DATA_DIR"] = str(data)
    os.environ["DATABASE_URL"] = f"sqlite:///{data / 'test.db'}"
    reset_settings_cache()
    return get_settings()


@pytest.fixture()
def db(_settings):
    """Fresh schema per test on a file-backed SQLite (needed for multi-connection behaviour)."""
    from app.db import Base, configure_engine, init_db, SessionLocal

    engine = configure_engine(_settings.database_url)
    Base.metadata.drop_all(engine)
    init_db(engine)
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def seed_clients(db):
    from app.clients.intake import load_intake_file, upsert_client

    clients = []
    for f in sorted((ROOT / "seed").glob("client_*.yaml")):
        clients.append(upsert_client(db, load_intake_file(f)))
    db.commit()
    return clients
