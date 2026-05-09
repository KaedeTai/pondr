# pondr success recipes

Concrete end-to-end flows that work in the current codebase. Each recipe
covers what you ask, what pondr does internally, and what to verify.

---

## Recipe 1 — Strategy synthesis from a hypothesis

**You say** (in chat or via `POST /api/topic`):

> 設計一個策略：當 BTCUSDT 在 1 分鐘內出現 >2σ 的成交量尖峰時，淡 (fade) 該方向，
> 持倉 5 分鐘後平倉。

**pondr does:**

1. The chat router classifies this as `queue_topic` and queues a
   research task. *Or*, if you instead let the bot find this idea itself,
   the synthesizer matches the hypothesis-shape regex
   (`pondr/research/strategy_synth.py:_HYPOTHESIS_SHAPE`) on a finding
   and auto-enqueues `[design_strategy] <hypothesis>`.
2. `research/loop.py` sees the `[design_strategy]` prefix and routes to
   `strategy_synth.run(task)`.
3. `strategy_synth` calls `tools.strategy.design_strategy(name, hypothesis)`.
   The LLM @ `127.0.0.1:9080/v1` writes the `on_tick(state, tick)` body
   following the prompt in `tools/strategy.py:STRATEGY_SYSTEM_PROMPT`.
4. The output is AST-validated by `quant/strategies/sandbox.py`. If the
   LLM tried `import os` or used `getattr`, the row is saved with
   `status='compile_error'` so it's still browsable in the lineage tree
   for a later iterate call to fix.
5. `run_strategy(strategy_id, symbol=<most-traded>)` pulls ~20k ticks
   from DuckDB, runs the sandboxed code with `asyncio.wait_for(timeout=5s)`
   plus per-50-tick cooperative yields, force-closes at the last price,
   computes annualised Sharpe (hourly-resampled) + MDD + fees, and writes
   a row to `backtests` with `strategy_id` FK populated.
6. If the first run produced trades, `_suggest_modification` asks the LLM
   for *one* targeted tweak ("add a 200-tick z-score gate", "drop qty to
   0.005") and `iterate_strategy(parent_id, mod)` saves a child variant.
   The child gets backtested too.
7. Both backtests + a 1-line summary land as a finding under
   `notes.topic = finding/strategy_synth/<name>`, visible in the dashboard
   *Recent findings* card.

**Verify:**

```bash
# strategy table populated
sqlite3 data/research_kb.db "SELECT id, name, status, lineage_parent_id
                              FROM strategies ORDER BY id DESC LIMIT 5;"

# linked backtests
sqlite3 data/research_kb.db "SELECT id, strategy, n_ticks,
                              json_extract(metrics_json,'$.sharpe') AS sharpe,
                              json_extract(metrics_json,'$.max_drawdown') AS dd
                              FROM backtests
                              WHERE strategy_id IS NOT NULL
                              ORDER BY id DESC LIMIT 5;"

# dashboard
open http://127.0.0.1:8090       # see 🧪 Strategy lab card
```

**Common failure modes:**

* `validation_error: imports not allowed in strategy code` — LLM emitted
  `import math`. The sandbox always pre-injects `math`/`np`/`stat`, but the
  LLM doesn't always trust that. Re-run `design_strategy`; if persistent,
  tighten the system prompt example.
* `error: no ticks for symbol/range` — the symbol has no rows in DuckDB.
  `_pick_symbol()` defaults to BTCUSDT; if the feed is offline you'll need
  to wait for it to reconnect (check `Market feeds` card).

---

## Recipe 2 — Manual strategy from the dashboard

You don't have to wait for the auto-flow to fire.

1. `open http://127.0.0.1:8090`, scroll to **🧪 Strategy lab**.
2. Click `+ design new strategy`, type a name + hypothesis, click `design`.
   POSTs to `/api/strategies/design` which calls the same
   `tools.strategy.design_strategy()` as the auto flow.
3. The new strategy appears in the list. Expand it; click `🔄 re-run` to
   POST `/api/strategies/<id>/run`.
4. Type a modification in the inline input and click `💬 iterate` —
   POSTs `/api/strategies/<id>/iterate` with the modification text.
5. Click `📜 code+lineage` to GET `/api/strategies/<id>` with the code,
   the full lineage tree, and the last 10 backtests.

---

## Recipe 3 — Compare a parent + iterations

After running design + a few iterates, ask in chat:

> compare strategies 12 13 14 — which Sharpe is best?

The router queues a `know_about` task; the executor sees
`compare_strategies` in its tool list and calls
`compare_strategies([12,13,14])`. The reply lists each strategy's last
backtest sharpe / mdd / pnl side by side.

---

## Notes / gotchas

* **Sandbox is not a security boundary.** It blocks the obvious mistakes
  (`import os`, `open()`, `__class__.__bases__`) but a determined attacker
  controlling LLM output could probably escape via an obscure path. Treat
  it as "stops the LLM from doing dumb stuff", not "safe to expose to the
  internet".
* **Per-tick timeout limitation.** `asyncio.wait_for` cannot interrupt a
  sync `while True: pass` *inside* one tick — Python doesn't yield to the
  event loop without an `await`. The protection works at per-tick
  granularity (cooperative yields every 50 ticks). For a single tick that
  hangs forever you'd need a thread-based watchdog; not currently
  implemented.
* **Position sizing differs by interface.** Legacy `Signal('buy', 1.0)`
  scales by `risk_pct × equity`. Dict-style `{'side':'buy','qty':0.05}`
  takes `qty` verbatim. Don't mix-and-match in a hypothesis you ship to
  the LLM.
