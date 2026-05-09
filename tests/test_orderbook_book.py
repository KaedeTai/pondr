"""OrderBook snapshot + delta application."""
from pondr.quant.orderbook.book import OrderBook, mid_price


def test_snapshot_sorts_correctly():
    b = OrderBook("test", "BTC")
    b.apply_snapshot(
        bids=[(99.0, 1.0), (100.0, 2.0), (98.0, 3.0)],
        asks=[(102.0, 1.0), (101.0, 2.0), (103.0, 3.0)])
    assert [l.price for l in b.bids] == [100.0, 99.0, 98.0]
    assert [l.price for l in b.asks] == [101.0, 102.0, 103.0]
    assert b.best_bid() == 100.0
    assert b.best_ask() == 101.0
    assert b.mid() == 100.5


def test_snapshot_drops_zero_size():
    b = OrderBook("t", "X")
    b.apply_snapshot(bids=[(100.0, 0.0), (99.0, 1.0)],
                     asks=[(101.0, 1.0), (102.0, 0.0)])
    assert len(b.bids) == 1
    assert len(b.asks) == 1


def test_delta_upsert_and_remove():
    b = OrderBook("t", "X")
    b.apply_snapshot(bids=[(100.0, 1.0)], asks=[(101.0, 1.0)])
    # update existing bid level
    b.apply_delta("bid", 100.0, 5.0)
    assert b.bids[0].size == 5.0
    # add new bid level
    b.apply_delta("bid", 99.5, 2.0)
    assert len(b.bids) == 2
    assert b.bids[0].price == 100.0  # still sorted
    # remove via size=0
    b.apply_delta("bid", 100.0, 0.0)
    assert len(b.bids) == 1
    assert b.bids[0].price == 99.5


def test_volumes_top_n():
    b = OrderBook("t", "X")
    b.apply_snapshot(
        bids=[(100, 1), (99, 2), (98, 4)],
        asks=[(101, 1), (102, 2), (103, 8)])
    bv, av = b.volumes_top_n(2)
    assert bv == 3.0  # 1 + 2
    assert av == 3.0  # 1 + 2


def test_mid_price_helper():
    assert mid_price(100, 101) == 100.5
    assert mid_price(None, 101) is None
    assert mid_price(100, None) is None


def test_is_fresh():
    import time
    b = OrderBook("t", "X")
    b.apply_snapshot([(100, 1)], [(101, 1)], ts=time.time())
    assert b.is_fresh(max_age_s=5.0)
    b.last_update_ts = time.time() - 1000
    assert not b.is_fresh(max_age_s=5.0)
