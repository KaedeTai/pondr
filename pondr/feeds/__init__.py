from .binance import BinanceFeed
from .coinbase import CoinbaseFeed
from .kraken import KrakenFeed
from .binance_depth import BinanceDepthFeed
from .coinbase_depth import CoinbaseDepthFeed
from .binance_aggtrade import BinanceAggTradeFeed
from .binance_depth_diff import BinanceDepthDiffFeed

ALL = [BinanceFeed, CoinbaseFeed]  # trade feeds (kraken optional)
DEPTH_ALL = [BinanceDepthFeed, CoinbaseDepthFeed]

# Auxiliary streams that don't fit the trade/depth-snapshot mould but still
# need a long-running task. Each feed exposes ``label``, ``msg_count``,
# ``connected`` and a ``run()`` coroutine.
AUX_ALL = [BinanceAggTradeFeed, BinanceDepthDiffFeed]
