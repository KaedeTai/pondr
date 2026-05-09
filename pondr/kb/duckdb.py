"""DuckDB KB — market tick time-series. Synchronous; we wrap in asyncio.to_thread."""
from __future__ import annotations
import asyncio
import duckdb
from .. import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS ticks (
  ts DOUBLE NOT NULL,
  source VARCHAR NOT NULL,
  symbol VARCHAR NOT NULL,
  price DOUBLE,
  qty DOUBLE,
  side VARCHAR
);
CREATE INDEX IF NOT EXISTS idx_ticks_sym ON ticks(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_ticks_src ON ticks(source, ts);

CREATE TABLE IF NOT EXISTS orderbook_imbalances (
  ts DOUBLE NOT NULL,
  source VARCHAR NOT NULL,
  symbol VARCHAR NOT NULL,
  ratio DOUBLE,
  bid_vol DOUBLE,
  ask_vol DOUBLE,
  levels INTEGER,
  mid_price DOUBLE
);
CREATE INDEX IF NOT EXISTS idx_obi_sym ON orderbook_imbalances(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_obi_src ON orderbook_imbalances(source, ts);

CREATE TABLE IF NOT EXISTS aggtrades (
  ts DOUBLE NOT NULL,
  source VARCHAR NOT NULL,
  symbol VARCHAR NOT NULL,
  agg_id BIGINT,
  first_trade_id BIGINT,
  last_trade_id BIGINT,
  price DOUBLE,
  qty DOUBLE,
  is_buyer_maker BOOLEAN
);
CREATE INDEX IF NOT EXISTS idx_aggtrades_sym ON aggtrades(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_aggtrades_src ON aggtrades(source, ts);

CREATE TABLE IF NOT EXISTS depth_diffs (
  ts DOUBLE NOT NULL,
  source VARCHAR NOT NULL,
  symbol VARCHAR NOT NULL,
  first_update_id BIGINT,
  final_update_id BIGINT,
  bids JSON,
  asks JSON
);
CREATE INDEX IF NOT EXISTS idx_depthdiff_sym ON depth_diffs(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_depthdiff_src ON depth_diffs(source, ts);
"""

_lock = asyncio.Lock()


def _conn():
    return duckdb.connect(str(config.DB_TICKS))


async def init():
    def _do():
        c = _conn()
        c.execute(SCHEMA)
        c.close()
    await asyncio.to_thread(_do)


async def insert_tick(source: str, symbol: str, price: float, qty: float, side: str, ts: float):
    async with _lock:
        def _do():
            c = _conn()
            c.execute(
                "INSERT INTO ticks(ts, source, symbol, price, qty, side) VALUES(?,?,?,?,?,?)",
                [ts, source, symbol, price, qty, side])
            c.close()
        await asyncio.to_thread(_do)


async def insert_many(rows: list[tuple]):
    if not rows:
        return
    async with _lock:
        def _do():
            c = _conn()
            c.executemany(
                "INSERT INTO ticks(ts, source, symbol, price, qty, side) VALUES(?,?,?,?,?,?)",
                rows)
            c.close()
        await asyncio.to_thread(_do)


async def count() -> int:
    def _do():
        c = _conn()
        n = c.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
        c.close()
        return int(n)
    return await asyncio.to_thread(_do)


async def query(sql: str) -> list[dict]:
    def _do():
        c = _conn()
        cur = c.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
        c.close()
        return [dict(zip(cols, r)) for r in rows]
    return await asyncio.to_thread(_do)


async def insert_orderbook_imbalances(rows: list[tuple]) -> None:
    """rows: (ts, source, symbol, ratio, bid_vol, ask_vol, levels, mid_price)."""
    if not rows:
        return
    async with _lock:
        def _do():
            c = _conn()
            c.executemany(
                "INSERT INTO orderbook_imbalances"
                "(ts, source, symbol, ratio, bid_vol, ask_vol, levels, mid_price)"
                " VALUES(?,?,?,?,?,?,?,?)", rows)
            c.close()
        await asyncio.to_thread(_do)


async def recent(symbol: str, since: float | None = None, limit: int = 100) -> list[dict]:
    if since is None:
        sql = f"SELECT ts, source, symbol, price, qty, side FROM ticks WHERE symbol='{symbol}' ORDER BY ts DESC LIMIT {limit}"
    else:
        sql = f"SELECT ts, source, symbol, price, qty, side FROM ticks WHERE symbol='{symbol}' AND ts >= {since} ORDER BY ts DESC LIMIT {limit}"
    return await query(sql)


# ---- aggTrade -----------------------------------------------------------

async def insert_aggtrade(source: str, symbol: str, agg_id: int,
                          first_trade_id: int, last_trade_id: int,
                          price: float, qty: float,
                          is_buyer_maker: bool, ts: float) -> None:
    async with _lock:
        def _do():
            c = _conn()
            c.execute(
                "INSERT INTO aggtrades(ts, source, symbol, agg_id, "
                "first_trade_id, last_trade_id, price, qty, is_buyer_maker) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                [ts, source, symbol, agg_id, first_trade_id, last_trade_id,
                 price, qty, bool(is_buyer_maker)])
            c.close()
        await asyncio.to_thread(_do)


async def count_aggtrades() -> int:
    def _do():
        c = _conn()
        n = c.execute("SELECT COUNT(*) FROM aggtrades").fetchone()[0]
        c.close()
        return int(n)
    return await asyncio.to_thread(_do)


async def recent_aggtrades(symbol: str, since: float | None = None,
                           limit: int = 100) -> list[dict]:
    if since is None:
        sql = (f"SELECT ts, source, symbol, agg_id, price, qty, "
               f"is_buyer_maker FROM aggtrades WHERE symbol='{symbol}' "
               f"ORDER BY ts DESC LIMIT {int(limit)}")
    else:
        sql = (f"SELECT ts, source, symbol, agg_id, price, qty, "
               f"is_buyer_maker FROM aggtrades WHERE symbol='{symbol}' "
               f"AND ts >= {float(since)} "
               f"ORDER BY ts DESC LIMIT {int(limit)}")
    return await query(sql)


# ---- Depth diff stream --------------------------------------------------

async def insert_depth_diff(source: str, symbol: str,
                            first_update_id: int, final_update_id: int,
                            bids_json: str, asks_json: str,
                            ts: float) -> None:
    async with _lock:
        def _do():
            c = _conn()
            c.execute(
                "INSERT INTO depth_diffs(ts, source, symbol, "
                "first_update_id, final_update_id, bids, asks) "
                "VALUES(?,?,?,?,?,?,?)",
                [ts, source, symbol, first_update_id, final_update_id,
                 bids_json, asks_json])
            c.close()
        await asyncio.to_thread(_do)


async def count_depth_diffs() -> int:
    def _do():
        c = _conn()
        n = c.execute("SELECT COUNT(*) FROM depth_diffs").fetchone()[0]
        c.close()
        return int(n)
    return await asyncio.to_thread(_do)


async def recent_depth_diffs(symbol: str, since: float | None = None,
                             limit: int = 100) -> list[dict]:
    if since is None:
        sql = (f"SELECT ts, source, symbol, first_update_id, "
               f"final_update_id, bids, asks FROM depth_diffs "
               f"WHERE symbol='{symbol}' ORDER BY ts DESC LIMIT {int(limit)}")
    else:
        sql = (f"SELECT ts, source, symbol, first_update_id, "
               f"final_update_id, bids, asks FROM depth_diffs "
               f"WHERE symbol='{symbol}' AND ts >= {float(since)} "
               f"ORDER BY ts DESC LIMIT {int(limit)}")
    return await query(sql)
