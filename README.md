# pondr — 自主研究機器人 (Quantitative Trading Specialist)

An always-on research bot that reads markets, browses the web, builds a
knowledge base, talks to you across multiple channels, and tells you when
it's stuck — instead of silently failing.

```
┌──────────────────────────── pondr ────────────────────────────┐
│  asyncio supervisor                                            │
│                                                                │
│   ┌─ research loop ─┐   ┌─ market WS pool ─┐  ┌─ REST polls ─┐ │
│   │ planner →       │   │  Binance         │  │  CoinGecko   │ │
│   │ executor →      │   │  Coinbase        │  │  FRED        │ │
│   │ synthesizer →   │   │  Kraken (opt)    │  │  CoinDesk    │ │
│   │ reflector       │   └────────┬─────────┘  └──────┬───────┘ │
│   └────────┬────────┘            │                   │         │
│            ▼                     ▼                   ▼         │
│   ┌──────── knowledge base ──────────────────────────────────┐ │
│   │  SQLite (research_kb.db): tasks, sources, notes,        │ │
│   │   pending_questions, user_preferences, capability_gaps, │ │
│   │   knowledge_gaps                                        │ │
│   │  DuckDB (market_ticks.db): tick time-series             │ │
│   │  ChromaDB: semantic embeddings                          │ │
│   └────────────────────────────────────────────────────────-┘ │
│            ▲                                  ▲                │
│            │                                  │                │
│   ┌─ Channel mux ──┐                  ┌─ Dashboard ─┐          │
│   │  WebSocket     │  ←──── chat ───→ │  FastAPI    │          │
│   │  stdio (tty)   │                  │  :8090      │          │
│   │  Telegram (opt)│                  │  + WS push  │          │
│   └────────────────┘                  └─────────────┘          │
│            ▲                                                   │
│            │ ask_user / capability_gap / findings              │
└────────────┴───────────────────────────────────────────────────┘
                    LLM: http://127.0.0.1:9080/v1
                    (gemma-4-31B-it-Q4_K_M.gguf, OpenAI-compat)
```

## Quick start

```bash
cd /Users/kaede/pondr
source .venv/bin/activate
bash scripts/start.sh
open http://127.0.0.1:8090       # dashboard
python scripts/chat.py            # CLI chat client
bash scripts/status.sh            # quick status snapshot
bash scripts/stop.sh              # stop the bot
```

The first run seeds six research topics under `量化交易策略與市場結構研究`
(經典策略 / 風險管理 / 市場微結構 / Crypto / Arxiv / tick anomaly). The bot
then runs forever — planning, browsing, synthesizing, reflecting — until you
stop it.

## Features

### Always-on autonomous research loop

The bot picks the next queued task and runs it through four stages:

1. **Planner** decomposes the topic into 3–6 subtasks.
2. **Executor** runs each subtask via LLM function-calling (web/RAG/SQL/
   market tools, see below). It is instructed to call `ask_user` when it
   genuinely needs a human decision.
3. **Synthesizer** writes a 1–3-sentence finding, then runs a second LLM
   call to score confidence 0.0–1.0 with a reason.
4. **Reflector** proposes 0–3 follow-up topics, which get queued.

State is persisted in SQLite, so a restart picks up exactly where it left
off (queued tasks, pending questions, preferences, knowledge gaps).

### Multi-channel communication (`pondr/server/channels/`)

A unified `MessageChannel` ABC + `ChannelMux` lets the bot broadcast to all
clients, race for the first answer, and merge inbound streams. Three
implementations ship:

- **WebSocketChannel** on `:8765` — accepts many simultaneous browser /
  CLI clients.
- **StdioChannel** — reads stdin when attached to a TTY; output-only when
  the bot runs under `nohup`.
- **TelegramChannel** — only active if `TELEGRAM_BOT_TOKEN` and
  `TELEGRAM_CHAT_ID` are set in `.env`.

Add to `PONDR_CHANNELS` in `.env` to control which start up.

### User-facing chat

Send any text via WS / stdio / Telegram. A small router LLM call
classifies intent and routes:

| Intent          | What happens                                         |
|-----------------|------------------------------------------------------|
| `interrupt`     | Sets a flag the research loop checks between subtasks |
| `queue_topic`   | Adds a research task                                 |
| `know_about`    | "What do you know about X" → notes + RAG + gaps      |
| `save_preference`| Long-lived instruction → persisted (see preferences) |
| `answer`        | Plain chat reply                                     |

You can also `/topic <text>` from the CLI and `/status` to see the queue.

### Bot-initiated questions (`ask_user`)

When the LLM hits ambiguity it doesn't want to guess on, it calls
`ask_user(question, options=None, timeout_s=None)`. The mux broadcasts to
every active channel; the first answer wins.

**Pending question persistence** — questions live in the
`pending_questions` SQLite table and survive bot restarts. On (re)connect:

- Every new WebSocket client gets the full pending list immediately after
  the welcome packet.
- Stdio and Telegram get the digest once per restart (tracked in
  `sent_to_channels` to avoid spam).

A background sweep marks `timeout_at`-expired rows as `timeout` and
resolves any awaiting future with `TimeoutError`. The dashboard's
**⚠️ Bot is asking** card shows qid prefix, age, asked_by, and a reply
input.

### Capability gap detection (`report_capability_gap`)

Any LLM call can self-report a missing capability (e.g. "fetch real-time
TradingView chart", "access user's brokerage", "OCR a scanned PDF").
Repeated reports of the same name bump `report_count` instead of spamming.

- Severity ≥ 4 triggers an immediate `ask_user("want to help install?")`.
- Dashboard **🔧 Capability gaps** card sorts by severity, with ✓ resolved
  / ✕ dismissed buttons.
- Endpoints: `GET /api/capability_gaps`,
  `POST /api/capability_gaps/{id}/status`.

### Confidence-aware findings

The synthesizer runs a second LLM call to score each finding 0.0–1.0 and
record a one-line reason. Numbers persist on the `notes` row
(`confidence`, `confidence_reason`, `source_count`) and on the ChromaDB
metadata.

- **Low confidence (< 0.5)** auto-enqueues a `[triangulate] …` task to
  find ≥2 independent sources.
- **Conflicts** detected during synth fire-and-forget an `ask_user`
  asking which source you trust.
- Dashboard **📌 Recent findings** card shows a colored bar (red < 50 %,
  yellow 50–80 %, green > 80 %) with the reason on hover.

### Knowledge gap map

Every `PONDR_KMAP_INTERVAL_S` (default 6 h) the bot reflects on each
active topic and asks the LLM to enumerate sub-questions, marking each as
`known` / `researching` / `unknown`. New unknowns auto-enqueue `[gap] …`
research tasks.

- Dashboard **📊 Knowledge map** card: collapsible per-topic tree
  (✓ green / ⚠ yellow / ❓ red) with global counts in the header.
- Chat: ask "what do you know about momentum?" — the bot replies with
  notes + RAG hits + known sub-questions + an explicit
  *"⚠️ I don't know:"* section listing unknowns.
- Endpoints: `GET /api/knowledge[?topic=…]`,
  `POST /api/knowledge/{id}/status`.

### Quant analytics — backtest, arb, orderbook imbalance

Three live quant pipelines feed both the dashboard and the LLM tools:

**Backtesting** (`pondr/quant/backtest/`, `pondr/quant/strategies/`)
A tick-replay engine consumes DuckDB ticks and runs strategy callables that
return `Signal('buy'|'sell'|'flat', size, reason)`. Built-in strategies:
`ma_cross`, `mean_reversion` (z-score), `breakout` — each ~80 lines, easy to
fork. Metrics: Sharpe, Sortino, max drawdown, win rate, profit factor, plus
an ASCII equity curve. Results land in SQLite `backtests` (with confidence,
auto-set to 0.3 when `n_ticks < 1000`). LLM tool: `run_backtest(strategy_name,
symbol, start_ts?, end_ts?)`. Dashboard **📈 Backtests** card lists all runs
with sparkline curves and metrics.

**Cross-exchange arb scanner** (`pondr/quant/arb/`)
A 1 Hz polling loop compares the latest tick on Binance and Coinbase for each
asset (BTC, ETH), computes gross spread in basis points, subtracts a
configurable per-side fee (default 10 bp), and writes any opportunity above
the net threshold (default 5 bp) to `arb_opportunities`. **Observation only —
never places orders.** LLM tool: `query_arb_history(symbol, min_spread_bp,
hours)`. Dashboard **💱 Arb opportunities** card lists buy/sell venue + gross
+ net + theoretical PnL.

**Orderbook imbalance** (`pondr/quant/orderbook/`,
`pondr/feeds/{binance,coinbase}_depth.py`)
Subscribes Binance `depth20@100ms` (top-20 snapshots) and Coinbase
`level2_batch` (snapshot + incremental updates) — note `level2` is deprecated
for new clients, so we use the batched feed. Maintains an in-memory
`OrderBook` per `(exchange, symbol)`, samples bid/ask volume ratio over the
top-20 levels every second, and persists to DuckDB
`orderbook_imbalances`. A sustained anomaly (`ratio > 3` or `< 0.33` for
≥30 s) fires an `orderbook_alert` event. LLM tools:
`query_orderbook_imbalance(symbol, hours, threshold)` and
`summarize_orderbook(symbol, window_min)`. Dashboard
**📊 Orderbook imbalances** card shows recent ratios per venue + symbol.

The knowledge map auto-detects gaps mentioning *sharpe / strategy / 回測 /
breakout / momentum* etc. and queues `[backtest] run_backtest(...)` tasks the
executor will pick up automatically.

### User preferences memory

Long-lived instructions ("請以後跟我說繁體中文", "always cite sources",
"don't ping me at night") get detected by the chat router and persisted
to `user_preferences` (SQLite) + `data/preferences.md` (human-editable
mirror).

Every subsequent LLM call — across all four research stages, the chat
router, and `ask_user` — has the active preferences prepended to its
system message:

```
# User preferences (always honor):
## communication
- language: 繁體中文
- tone: 簡潔
## workflow
- ...
```

Sensitive content (api keys / passwords / 密碼 / SSN / credit card …) is
regex-blocked from being persisted. History is audited in
`user_preferences_history`. Dashboard **User preferences** card has
inline add/delete; you can also edit `data/preferences.md` by hand and
the table reloads via `prefs_kb.init()` on next start.

### Dashboard (`http://127.0.0.1:8090/`)

A single-page FastAPI app, auto-refresh every 3 s with a `/ws/state` push
channel for live LLM logs. Sections (top-down):

- **Top bar**: status indicator, uptime, current task, total LLM tokens,
  event count, current research seed
- **⚠️ Bot is asking** (only when pending Qs exist)
- **📊 Knowledge map** with per-topic ✓/⚠/❓ tree
- **📌 Recent findings** with confidence bars
- **📈 Backtests** with ASCII equity curves
- **💱 Arb opportunities** — cross-exchange spread alerts
- **📊 Orderbook imbalances** — bid/ask ratio per exchange/symbol
- **🔧 Capability gaps** sorted by severity
- **User preferences** with inline edit/delete
- **LLM I/O log live tail** — last 50 calls, expandable, shows full
  prompt + response + tool calls + latency + tokens
- **Live tasks** (running / queued / done)
- **Market feeds** (per-feed connected + tick count + last tick)
- **Knowledge base** (notes / sources / RAG chunks / ticks / DB sizes)
- **Channels** status
- **Event timeline** (last 30)
- **Chat** embedded ws client + topic-add input

### Market data feeds (`pondr/feeds/`)

Each feed runs as its own asyncio task with exponential-backoff
auto-reconnect:

- **Binance** — `wss://stream.binance.com:9443/...` BTCUSDT/ETHUSDT trades
- **Coinbase** — `wss://ws-feed.exchange.coinbase.com` BTC-USD/ETH-USD
  matches
- **Kraken** — optional (in `pondr/feeds/kraken.py`, not enabled by
  default)

Every tick is written to DuckDB (`data/market_ticks.db`) with
`ts, source, symbol, price, qty, side`. The bot can query it via
`read_market_ticks(symbol, since, limit)` and
`summarize_market(symbol, window_min)` tools.

### Periodic REST polls (`pondr/polls/`)

- **CoinGecko top-20** every 5 min — caches symbol/price snapshot to a
  KB note
- **FRED yield curve** (DGS2, DGS10) every 60 min — only if
  `FRED_API_KEY` is set
- **CoinDesk RSS** every 15 min — headline digest

### LLM-callable tools (`pondr/tools/`)

| Tool                      | Purpose                                 |
|---------------------------|-----------------------------------------|
| `web_search(q, n)`        | DuckDuckGo                              |
| `web_fetch(url)`          | httpx + BeautifulSoup readable text     |
| `browser_fetch(url)`      | Playwright (falls back to `web_fetch`)  |
| `rest_call(url, method…)` | Generic HTTP                            |
| `rag_search(q, k)`        | ChromaDB                                |
| `rag_store(text, meta)`   | ChromaDB write                          |
| `sql_query(db, sql)`      | Read-only SELECT/WITH on `kb` or `ticks`|
| `note_write` / `note_list`| Topic-keyed notes                       |
| `read_market_ticks`       | Recent ticks for a symbol               |
| `summarize_market`        | Window stats (mean/std/min/max/return)  |
| `pref_list/save/delete/search` | User preferences from inside LLM   |
| `ask_user`                | Block until the user replies            |
| `report_capability_gap`   | Self-report missing capability          |
| `run_backtest`            | Replay a strategy over historical ticks |
| `query_arb_history`       | Past cross-exchange arb opportunities   |
| `query_orderbook_imbalance` / `summarize_orderbook` | Orderbook stats |
| `interrupt_check`         | Has the user requested interrupt?       |

All schemas live in each tool module's `SCHEMA` dict and are auto-
collected in `pondr.tools.__init__.ALL_SCHEMAS`.

### Storage layers

- **`data/research_kb.db`** (SQLite): `tasks`, `sources`, `notes`,
  `decisions`, `pending_questions`, `user_preferences` +
  `user_preferences_history`, `capability_gaps`, `knowledge_gaps`, `backtests`, `arb_opportunities`
- **`data/market_ticks.db`** (DuckDB): `ticks` table + `orderbook_imbalances` with indexes
  on `(symbol, ts)` and `(source, ts)`
- **`data/chroma/`**: ChromaDB persistent collection `pondr_kb`
- **`data/raw/`**: room for downloaded HTML/PDF snapshots
- **`data/preferences.md`**: human-readable mirror of `user_preferences`
- **`data/logs/`**:
  - `llm_io.jsonl` — one JSON line per LLM call
  - `events.jsonl` — major events (boot, task_start, finding,
    capability_gap_reported, …)
  - `pondr.log` — loguru rotating file log
  - `stdout.log` / `stderr.log` — captured process output


### Quant analytics — backtesting, arb, orderbook (`pondr/quant/`)

Three deep features that turn the bot from a pure researcher into a
quantitative analyst on the live market data it's already collecting.

**Backtesting framework** (`pondr/quant/backtest/`):
- Tick-replay engine (`engine.py`) consumes a historical tick stream
  through any strategy callable, tracking signed position, realized +
  unrealized PnL, fees (10 bp/side default), and per-tick equity.
- Metrics (`metrics.py`): annualized Sharpe, Sortino, max drawdown,
  win rate, profit factor.
- Markdown report (`report.py`) with an inline ASCII equity curve.
- Three starter strategies (`pondr/quant/strategies/`): `ma_cross`
  (short/long MA crossover), `mean_reversion` (z-score fade), `breakout`
  (range breakout). Each ~80 lines and easy for the LLM to mutate.
- LLM tool **`run_backtest(strategy_name, symbol, start_ts, end_ts,
  max_ticks)`** queries DuckDB ticks for the symbol, runs the engine
  in a worker thread, persists to the `backtests` SQLite table
  (id / strategy / symbol / metrics_json / equity_blob / ascii_curve /
  report_md / confidence). Sub-1000-tick results auto-flag
  `low_confidence=True`; confidence scales 0.3 → 0.9 from 1K to 50K ticks.
- Dashboard **📈 Backtests** card with collapsible per-result detail
  + ASCII curve. Endpoints: `/api/backtests`, `/api/backtests/{id}`.

**Cross-exchange arb scanner** (`pondr/quant/arb/scanner.py`):
- Continuously compares the latest Binance and Coinbase ticks for
  shared symbols (BTC / ETH), normalizing names (`BTCUSDT` ↔ `BTC-USD`)
  and applying a per-side fee model (default 10 bp, configurable via
  `PONDR_ARB_FEE_BP`).
- When net spread (gross spread − 2× fees) clears
  `PONDR_ARB_THRESHOLD_BP` (default 5 bp) it writes to the
  `arb_opportunities` table with buy/sell exchange, prices, gross/net
  spread, and theoretical notional PnL.
- LLM tool **`query_arb_history(symbol, min_spread_bp, since)`**.
- Dashboard **💱 Arb opportunities (24h)** card.
- *Pure observation — never places orders.*

**Orderbook imbalance detector** (`pondr/quant/orderbook/`):
- New depth-channel feeds:
  - `pondr/feeds/binance_depth.py` — `btcusdt@depth20@100ms` /
    `ethusdt@depth20@100ms` (pre-aggregated top 20 each tick).
  - `pondr/feeds/coinbase_depth.py` — `level2_batch` channel
    (snapshot + batched `l2update` messages).
- `book.py` maintains an in-memory `OrderBook` per
  `(exchange, symbol)` with sorted bids/asks (top N kept).
- `imbalance.py` periodically (1 s default) computes the
  bid/ask volume ratio over the top N levels and writes to the DuckDB
  `orderbook_imbalances` table; sustained anomaly (ratio > 3 or < 1/3
  for ≥ 30 s) fires an alert through the channel mux.
- LLM tools **`query_orderbook_imbalance(symbol, since, threshold)`**
  and **`summarize_orderbook(symbol, window_min)`**.
- Dashboard **📊 Orderbook imbalances** card with the most recent
  ratios per `(exchange, symbol)`.

These features integrate with the rest of the bot:
- Knowledge map can enqueue tasks like *"what's the Sharpe of MA cross
  on BTC?"* → research loop calls `run_backtest` → finding stored.
- Capability gap: if the LLM wants a strategy/exchange that isn't
  implemented, it `report_capability_gap`s instead of failing silently.
- Confidence scoring already-in-place applies to backtest findings.

### LLM provider

OpenAI-compatible chat completions, default `http://127.0.0.1:9080/v1`,
model `gemma-4-31B-it-Q4_K_M.gguf` (e.g. via `llama-server`). Swap in any
provider via `.env`. Every call — request, response, tool calls,
latency, tokens — is logged to `data/logs/llm_io.jsonl` and broadcast to
the dashboard's live tail.

## Configuration

### `.env` (optional, copied from `.env.example`)

```
PONDR_LLM_BASE_URL=http://127.0.0.1:9080/v1
PONDR_LLM_API_KEY=local
PONDR_LLM_MODEL=gemma-4-31B-it-Q4_K_M.gguf
PONDR_DASHBOARD_PORT=8090
PONDR_WS_PORT=8765
PONDR_INITIAL_TOPIC=量化交易策略與市場結構研究
PONDR_CHANNELS=ws,stdio                    # comma list; telegram auto-added if token set
PONDR_KMAP_INTERVAL_S=21600                # knowledge-map reflection cadence
PONDR_ARB_THRESHOLD_BP=5                   # net spread alert threshold
PONDR_ARB_FEE_BP=10                        # assumed per-side fee (bp)
PONDR_OB_INTERVAL_S=1                      # orderbook imbalance compute cadence
PONDR_OB_TOP_N=20                          # levels included in imbalance
FRED_API_KEY=                              # optional
TELEGRAM_BOT_TOKEN=                        # optional
TELEGRAM_CHAT_ID=                          # required if token is set
```

### `data/preferences.md`

Auto-created with a starter on first run; you can edit it by hand. The
SQLite table is the source of truth, but `_sync_md()` keeps the file in
sync after every write.

## Project structure

```
pondr/
├── pyproject.toml, README.md, .env.example, .gitignore
├── pondr/
│   ├── __init__.py, __main__.py            # asyncio supervisor
│   ├── config.py                           # .env-backed settings
│   ├── llm.py                              # OpenAI-compat client + prefs injection
│   ├── runtime.py                          # shared registry (feeds list)
│   ├── kb/
│   │   ├── sqlite.py                       # tasks/notes + migration
│   │   ├── duckdb.py                       # ticks
│   │   ├── chroma.py                       # semantic
│   │   ├── questions.py                    # pending_questions
│   │   ├── preferences.py                  # user_preferences + .md sync
│   │   ├── capability_gaps.py              # capability_gaps
│   │   └── knowledge_gaps.py               # knowledge_gaps
│   ├── tools/
│   │   ├── web_search.py, web_fetch.py, browser.py
│   │   ├── rest.py, rag.py, sql.py
│   │   ├── notes.py, market.py
│   │   ├── ask.py                          # ask_user
│   │   ├── prefs.py                        # pref_*
│   │   └── capability.py                   # report_capability_gap
│   ├── feeds/binance.py, coinbase.py, kraken.py,
│   │           binance_depth.py, coinbase_depth.py
│   ├── polls/coingecko.py, fred.py, news_rss.py
│   ├── research/
│   │   ├── loop.py                         # main research loop
│   │   ├── planner.py, executor.py
│   │   ├── synthesizer.py                  # finding + confidence + triangulate
│   │   ├── reflector.py
│   │   └── knowledge_map.py                # periodic self-reflection
│   ├── quant/
│   │   ├── backtest/{engine,metrics,report}.py
│   │   ├── strategies/{ma_cross,mean_reversion,breakout}.py
│   │   ├── arb/scanner.py
│   │   └── orderbook/{book,imbalance}.py
│   ├── server/
│   │   ├── dashboard.py                    # FastAPI + /api/* + /ws/state
│   │   ├── interrupt.py
│   │   ├── ws_server.py                    # thin compat wrapper
│   │   └── channels/
│   │       ├── base.py                     # MessageChannel ABC + Mux + ask_user
│   │       ├── websocket.py
│   │       ├── stdio.py
│   │       └── telegram.py
│   ├── templates/dashboard.html
│   └── utils/log.py, llm_log.py, retry.py, rate_limit.py
├── tests/                                  # pytest, all async-aware
└── scripts/start.sh, stop.sh, status.sh, chat.py
```

## Scripts

| Script               | Purpose                                            |
|----------------------|----------------------------------------------------|
| `scripts/start.sh`   | Start the bot in the background (`nohup`), write pidfile |
| `scripts/stop.sh`    | Read pidfile, SIGTERM then SIGKILL after 5 s       |
| `scripts/status.sh`  | Show running PID + first 30 lines of `/api/state`  |
| `scripts/chat.py`    | CLI WebSocket client (`/topic …`, `/status`, plain chat) |

## Logging

- **`data/logs/pondr.log`** — main log, loguru rotating (10 MB × 5)
- **`data/logs/llm_io.jsonl`** — every LLM call: `{ts, kind, model,
  messages, tools, response, function_calls, latency_ms, tokens,
  trace_id}`
- **`data/logs/events.jsonl`** — major events: `boot`, `task_start`,
  `finding`, `capability_gap_reported`, `ask_user`, `knowledge_map_run`, …
- **`data/logs/stdout.log`** / **`stderr.log`** — captured by `nohup`

## Testing

```bash
source .venv/bin/activate
python -m pytest tests/                     # 38 pass + 2 skipped while bot runs
```

The 2 skipped are DuckDB-touching tests that yield to the bot's
exclusive lock on `market_ticks.db`. Stop the bot first if you want
zero skips.

## Troubleshooting

- **DuckDB lock errors** — only one process can open `market_ticks.db`.
  Stop the bot before running tests that touch it (or use the skip
  fallback already in place).
- **No LLM responses** — confirm `curl http://127.0.0.1:9080/v1/models`
  returns 200. The bot logs `chat_completion_error` and keeps going.
- **Telegram silent** — check `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`
  in `.env`; the channel logs "telegram: no token/chat_id, channel
  disabled" on startup if either is missing.
- **Stale pendings flooding chat** — answer or dismiss them via
  dashboard, or `UPDATE pending_questions SET status='cancelled' WHERE …`.
- **Browser fetch always falls back** — `playwright install chromium` is
  optional and not run by default.

## License / Contributing

Private personal project. No license declared.
