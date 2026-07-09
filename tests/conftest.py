"""Pytest fixtures: an isolated temp DB and unique per-test guild ids.

Env is set before importing db_utils (which reads DB_FILE at import time), and the
repo root is put on sys.path so `import db_utils` / `bracket` / `main` resolve when
pytest is run from anywhere.
"""
import os
import sys
import tempfile
import itertools

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_FILE", os.path.join(tempfile.gettempdir(), "qotd_pytest.db"))
os.environ.setdefault("DISCORD_TOKEN", "test-token")

import pytest
import db_utils


@pytest.fixture(scope="session", autouse=True)
def _fresh_db():
    path = os.environ["DB_FILE"]
    if os.path.exists(path):
        os.remove(path)
    db_utils.init_db()
    yield


_guild_counter = itertools.count(100000)


@pytest.fixture
def gid():
    """A unique guild id with a config row already created (isolates tests)."""
    g = next(_guild_counter)
    db_utils.get_config(g)
    return g
