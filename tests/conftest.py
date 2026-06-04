"""Shared test fixtures for DebridNZBd tests.

Provides common fixtures used across multiple test modules:
- tmp_db: A fresh Database instance in a temp directory
- app_client: A FastAPI TestClient with an initialized database
"""

import pytest
import pytest_asyncio
from pathlib import Path

from debridnzd.db.database import Database


@pytest_asyncio.fixture
async def tmp_db(tmp_path: Path) -> Database:
    """Create a fresh database in a temporary directory.

    Yields the initialized Database instance and closes it after the test.
    Each test gets its own isolated database.
    """
    db_path = tmp_path / "test.db"
    database = Database(db_path)
    await database.initialize()
    yield database
    await database.close()