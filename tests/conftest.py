"""Test isolation: redirect all KB writes to a tmp dir before pondr imports."""
import os
import tempfile
import shutil
import asyncio
import pytest

# CRITICAL: set PONDR_DATA_DIR before any pondr.* import so config.py picks it up
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="pondr_test_data_")
os.environ["PONDR_DATA_DIR"] = _TEST_DATA_DIR

# Now safe to import pondr modules — they will use the tmp data dir
from pondr.kb import (
    sqlite as kb_sql,
    duckdb as ddb,
    chroma,
    questions as q_kb,
    preferences as prefs_kb,
    capability_gaps as cap_kb,
    knowledge_gaps as kg_kb,
    curriculum as curr_kb,
    strategies as strat_kb,
)


@pytest.fixture(autouse=True, scope="session")
def _kb_init():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(kb_sql.init())
    try:
        loop.run_until_complete(ddb.init())
    except Exception:
        pass  # bot may also be running and holding the lock; OK to skip in test
    loop.run_until_complete(chroma.init())
    loop.run_until_complete(q_kb.init())
    loop.run_until_complete(prefs_kb.init())
    loop.run_until_complete(cap_kb.init())
    loop.run_until_complete(kg_kb.init())
    try:
        loop.run_until_complete(curr_kb.init())
    except Exception:
        pass
    loop.run_until_complete(strat_kb.init())
    yield
    # tear down: remove tmp dir after session ends
    shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)
