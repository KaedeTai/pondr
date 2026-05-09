"""Feed/poller import + DuckDB write smoke."""
import asyncio
from pondr.feeds import BinanceFeed, CoinbaseFeed
from pondr.kb import duckdb as ddb


def aio(c):
    return asyncio.get_event_loop().run_until_complete(c)


def test_feeds_construct():
    b = BinanceFeed(); c = CoinbaseFeed()
    assert b.name == "binance" and c.name == "coinbase"
    assert b.tick_count == 0 and c.tick_count == 0


def test_duckdb_insert_and_count():
    import pytest, _duckdb
    try:
        aio(ddb.init())
        n0 = aio(ddb.count())
        aio(ddb.insert_tick("test", "TESTUSD", 100.0, 1.0, "buy", 1234567890.0))
        n1 = aio(ddb.count())
    except _duckdb.IOException:
        pytest.skip("market_ticks.db is locked (bot running)")
    assert n1 == n0 + 1
