"""ImbalanceDetector: ratio computation + sustained-anomaly detection."""
from __future__ import annotations
import time

from pondr.quant.orderbook.book import OrderBook
from pondr.quant.orderbook.imbalance import (
    compute_imbalance, ImbalanceDetector)


def test_compute_imbalance_balanced():
    b = OrderBook("t", "X")
    b.apply_snapshot(bids=[(100, 1), (99, 1)], asks=[(101, 1), (102, 1)])
    s = compute_imbalance(b, n=5)
    assert s["ratio"] == 1.0
    assert s["bid_vol"] == 2.0
    assert s["ask_vol"] == 2.0
    assert s["mid"] == 100.5


def test_compute_imbalance_bid_heavy():
    b = OrderBook("t", "X")
    b.apply_snapshot(bids=[(100, 10)], asks=[(101, 2)])
    s = compute_imbalance(b, n=5)
    assert s["ratio"] == 5.0


def test_compute_imbalance_no_asks():
    b = OrderBook("t", "X")
    b.apply_snapshot(bids=[(100, 5)], asks=[])
    s = compute_imbalance(b, n=5)
    assert s["ratio"] == float("inf")


def test_anomaly_requires_sustained_duration():
    """Single high-ratio sample → no alert. After 30s sustained → alert."""
    det = ImbalanceDetector(sample_interval_s=0.0,
                            high_thr=3.0, low_thr=0.33,
                            alert_duration_s=30.0)
    key = ("test", "X")
    t0 = 1000.0
    # First high sample: no alert yet
    det._update_anomaly(key, t0, 5.0)
    assert det.alerts_fired == 0
    # 5s later still high — duration <30s → no alert
    det._update_anomaly(key, t0 + 5, 4.5)
    assert det.alerts_fired == 0
    # 31s later → triggers alert
    det._update_anomaly(key, t0 + 31, 4.5)
    assert det.alerts_fired == 1


def test_anomaly_resets_when_balanced():
    det = ImbalanceDetector(high_thr=3.0, low_thr=0.33, alert_duration_s=30.0)
    key = ("test", "X")
    det._update_anomaly(key, 1000.0, 5.0)
    assert key in det._anom
    # ratio drops back into normal range → state cleared
    det._update_anomaly(key, 1010.0, 1.2)
    assert key not in det._anom


def test_anomaly_only_fires_once_per_episode():
    det = ImbalanceDetector(high_thr=3.0, low_thr=0.33, alert_duration_s=10.0)
    key = ("test", "X")
    det._update_anomaly(key, 1000.0, 5.0)
    det._update_anomaly(key, 1011.0, 5.0)  # → fires (duration=11>=10)
    det._update_anomaly(key, 1020.0, 5.0)  # already alerted, no second fire
    det._update_anomaly(key, 1030.0, 5.0)
    assert det.alerts_fired == 1


def test_anomaly_changes_direction():
    det = ImbalanceDetector(high_thr=3.0, low_thr=0.33, alert_duration_s=10.0)
    key = ("test", "X")
    det._update_anomaly(key, 1000.0, 5.0)         # bid_heavy
    det._update_anomaly(key, 1005.0, 0.1)         # flips ask_heavy → reset
    anom = det._anom[key]
    assert anom.direction == "ask_heavy"
    assert anom.started_ts == 1005.0
