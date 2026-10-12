# Scoping: AI-driven finance ("Markets") for this Open WebUI fork

Date: 2026-10-12. Scope: personal, single-user, self-hosted. Answers "is it possible, and what would it take" for stock + crypto trading, strategy authoring and execution, and market analysis inside this app.

> **Note on references.** File and line references are approximate and may drift. The lorebook / roleplay files cited as patterns live on the roleplay branch, not on `main`. On `main`, use `backend/open_webui/models/automations.py` and `backend/open_webui/migrations/versions/d4e5f6a7b8c9_add_automation_tables.py` as the model + migration pattern instead.

## 1. Verdict

Yes, and the fork is unusually well prepared for it. The pieces a trading feature needs (LLM tool-calling, encrypted credential storage, a human-approval gate on tool calls, an rrule scheduler with an audit table, Socket.IO push, a code interpreter, an MCP client, and a proven "add a big feature" pattern from the roleplay work) already exist. The work is mostly new domain code (data adapters, a broker layer, a strategy runtime, a risk gate) plus new UI, not plumbing.

Two decisions shape everything else:

1. **The LLM is an analyst and author, not the trading loop.** Research through 2026 (Alpha Illusion, StockBench, Look-Ahead-Bench) shows end-to-end LLM traders generally underperform buy-and-hold and suffer look-ahead leakage from pretraining. The sound design is: LLM writes and critiques strategies, explains data, and proposes trades; deterministic code backtests, enforces risk limits, and executes.
2. **Paper by default, live is opt-in per connection and per order.** Live orders go through the existing tool-approval pause so you click Approve in the chat before anything hits a broker.

## 2. Recommended stack

| Layer | Choice | Why |
|---|---|---|
| Primary broker (stocks, options, crypto) | **Alpaca** via `alpaca-py` | Static API keys (no weekly re-login), paper environment mirrors live, free real-time IEX websocket, maintained SDK, official MCP server. Crypto only in ~25 states. |
| Crypto venue | **Coinbase Advanced Trade** through **CCXT** | NY-licensed, official SDK; CCXT makes Kraken/Gemini swappable. Coinbase sandbox is mocked, so dry-run crypto on Alpaca paper or Gemini sandbox. |
| Market data | Alpaca (bars/quotes/stream) + **Massive** Starter $29/mo (history) + **SEC EDGAR** (free) + **FMP** (fundamentals) + **Tiingo** (news) + CoinGecko (crypto metadata) | All personal-use tiers. Avoid yfinance in the bot path (heavy 429 blocking in 2026). |
| Backtest engine | **vectorbt 1.1.x** (primary), **backtesting.py** (simple `Strategy` class path) | vectorbt: active, Python 3.11 to 3.14, in-process, fast for parameter sweeps. Note licences: vectorbt is Apache + Commons Clause (fine for personal use), backtesting.py is AGPL. |
| Live-trading engine upgrade path | nautilus_trader (phase 3+) | One codebase backtest to live, but heavy and mid v2 rewrite. |
| Crypto bot engine (optional) | **freqtrade** as a Docker sidecar via its REST API | Mature, monthly releases, `IStrategy` is a well-documented target for LLM-authored code, FreqAI for ML. GPL is fine as a separate process. |
| Technical analysis | TA-Lib official wheels + `pandas-ta-classic` | Original pandas-ta is unmaintained. |
| Charts | **TradingView Lightweight Charts v5** used directly in Svelte (the Svelte wrapper is stale) | Canvas, 45 kB, panes and trade markers built in. Needs the attribution logo. ECharts for portfolio analytics. |
| Zero-code starting point | **Alpaca MCP server** + **OpenBB MCP server** connected through the existing MCP client | Validates the whole idea in an afternoon before any code is written. |

Interactive Brokers (`ib_async`), Tradier and Public.com are reasonable second venues. Schwab has no paper trading and needs a manual weekly OAuth login. Robinhood's official API is crypto-only; its stock wrappers violate terms.

## 3. How it maps onto this codebase

| Need | Existing mechanism | Where |
|---|---|---|
| LLM can call finance functions | Built-in tool categories gated by `is_builtin_tool_enabled(<category>)` + a global `Config` flag + user permission | `backend/open_webui/utils/tools.py:520-760`, `backend/open_webui/tools/builtin.py` |
| Store broker/data API keys encrypted | Tool valves are Fernet-encrypted at rest (`encrypt_valves` / `decrypt_valves`) | `backend/open_webui/utils/valves.py` |
| Human confirms before an order | Chat-level `tool_approval_mode = 'ask'` pauses the run and persists `requires_approval` state; `ask_user` built-in tool for inline questions | `backend/open_webui/utils/middleware.py:3629`, `backend/open_webui/utils/tool_approval.py`, `tools/builtin.py:518` |
| Run things on a schedule | Automations: rrule-based worker loop that runs a prompt through the full chat pipeline; `AutomationRun` audit table; `timer` tool for one-shot delays | `backend/open_webui/utils/automations.py:64-126,345`, `models/automations.py` |
| Push live data to the browser | Socket.IO with per-user rooms (`user:{id}`), `emit_to_users` | `backend/open_webui/socket/main.py:356` |
| Run user Python (backtests, notebooks) | Code interpreter (pyodide in-browser, or a Jupyter server), external terminal servers | `backend/open_webui/utils/code_interpreter.py`, `routers/terminals.py` |
| Plug in external finance tool servers | MCP client (streamable HTTP) + OpenAPI tool servers | `backend/open_webui/utils/mcp/client.py`, `utils/tools.py:1473` |
| New DB tables | SQLAlchemy async models + alembic migration with `inspector` guard | pattern: `models/lorebooks.py`, `migrations/versions/7e3a9c2b51d4_add_lorebook_table.py` (roleplay branch; on `main` use `models/automations.py`, `d4e5f6a7b8c9_add_automation_tables.py`) |
| Feature flag + permission | `ENABLE_*` PersistentConfig, `user.permissions.features.*`, surfaced in `/api/config` features | `config.py:2019-2029, 3148-3160`, `main.py:2368` |
| Event bus / webhooks | `EVENTS` definitions + `publish_event`, outbound webhooks and notification targets | `backend/open_webui/events.py`, `routers/notifications.py` |
| New sidebar/workspace pages | SvelteKit routes under `src/routes/(app)/…`, sidebar entries gated on `$config.features` | `src/lib/components/layout/Sidebar.svelte:159-205`, `src/routes/(app)/workspace/+layout.svelte` |
| Charts | `chart.js` is already a dependency; no candlestick lib yet | `package.json` |
| Tests | pytest + pytest-asyncio, `backend/open_webui/test/` (roleplay tests are the local pattern) | `pyproject.toml:209` |

Gaps that need new code:

- **Per-tool approval.** `tool_approval_mode` is per chat, not per tool. Live-order tools need a "always requires approval" flag so the gate cannot be bypassed by a chat setting. Small change in `utils/middleware.py` where function calls are dispatched.
- **CPU-bound work.** The backend is one asyncio process. vectorbt/numba backtests must run in a `ProcessPoolExecutor` (or a worker container) so they do not stall chat.
- **Pyodide cannot run vectorbt/TA-Lib.** Strategy code must execute server-side (new executor) or in a Jupyter server configured via `CODE_INTERPRETER_JUPYTER_URL`.
- **Scheduler granularity.** The automation loop polls every `TIMER_POLL_INTERVAL` seconds and runs prompts through the chat pipeline. Fine for daily/hourly jobs. A bar-driven strategy runner needs its own loop fed by the data websocket, not an rrule.
- **No candlestick charting** in the frontend today.
- **i18n.** Every UI string lands in 65 locale files via `npm run i18n:parse` (the roleplay commit added ~230 lines per locale).

## 4. Feature breakdown

### 4.1 Connections and settings (new)
- `finance_connection` table: provider (`alpaca`, `coinbase`, `kraken`, `massive`, `fmp`, ...), kind (`broker` | `data`), encrypted credentials (reuse `encrypt_valves`), `is_paper` flag, state restrictions, created/updated.
- Admin/user settings page: add connection, test connection, toggle paper/live, risk limits (max position size, max daily loss, max open orders, allowed symbols, trading hours), global kill switch.
- Feature flag `ENABLE_FINANCE` + permission `features.finance`, surfaced in `/api/config`.

### 4.2 Market data service (new)
- `utils/finance/data/` with a `DataProvider` protocol: `bars(symbol, timeframe, start, end)`, `quote`, `search`, `fundamentals`, `news`, `stream(symbols)`.
- Adapters: Alpaca (stocks + crypto, REST + websocket), Massive, Tiingo, FMP, EDGAR, CoinGecko, CCXT (crypto OHLCV from any exchange).
- Local cache of bars in a `finance_bar` table (or Parquet files under `data/finance/`) keyed by provider/symbol/timeframe so backtests do not re-hit rate limits.
- Streaming: one upstream websocket per provider, fan-out to `user:{id}` Socket.IO room as `finance:quote` events.

### 4.3 Built-in tools, category `finance` (new, in `tools/builtin.py`)
Read-only (no approval): `get_quote`, `get_bars`, `search_symbols`, `get_fundamentals`, `get_filings`, `get_news`, `get_portfolio`, `get_positions`, `get_orders`, `list_strategies`, `get_backtest_result`, `compute_indicators`.
Mutating (approval always): `place_order`, `cancel_order`, `deploy_strategy`, `stop_strategy`, `update_risk_limits`.
Compute: `run_backtest(strategy_id, symbols, start, end, params)` returns metrics plus a result id the UI renders.
This is what lets you type "backtest a 20/50 EMA crossover on QQQ since 2020 and compare to buy-and-hold" in a normal chat.

### 4.4 Strategies (new workspace section, modelled on Tools)
- `finance_strategy` table: name, Python source, params schema, version history, universe, timeframe, engine (`vectorbt` | `backtesting.py` | `freqtrade`), meta.
- Loaded with the same frontmatter/`load_*_module_by_id` machinery as Tools, executed only in the worker pool.
- Editor page with CodeMirror (already in the app), params form, "Ask the model to improve this" button that opens a chat with the strategy and last backtest attached.
- Starter templates: EMA/SMA cross, RSI mean reversion, momentum rotation, Bollinger breakout, DCA.

### 4.5 Backtesting (new)
- `finance_backtest` table: strategy id, params, universe, range, status, metrics JSON (CAGR, Sharpe, Sortino, max drawdown, win rate, exposure, trade count), equity curve, trade list, and the buy-and-hold benchmark alongside every result.
- Walk-forward and parameter grid runs (vectorbt is built for this).
- Point-in-time discipline: strategies only see data up to the current bar; the LLM only sees results, never future bars, when asked to critique.

### 4.6 Bots / live and paper execution (new)
- `finance_bot` table: strategy id, connection id, params, mode (`paper` | `live`), status, heartbeat, last error.
- `bot_runner_loop` next to `automation_worker_loop`: subscribes to bars, calls the strategy's `on_bar`, passes intents through the **risk gate**, submits via the broker adapter, records to `finance_order` and `finance_fill`.
- Risk gate is pure Python, server-side, LLM cannot reach around it: position sizing caps, daily loss stop, symbol allowlist, market-hours check, duplicate-order guard, global kill switch.
- Alerts through the existing notification targets and webhooks (`EVENTS.FINANCE_ORDER_FILLED`, `FINANCE_RISK_TRIPPED`, `FINANCE_BOT_ERROR`).
- Crypto alternative: freqtrade sidecar; the bot runner becomes a thin proxy that pushes the strategy file and polls `/api/v1/status`.

### 4.7 Analysis and reporting
- Automations already do this: a daily 6:30 ET automation "summarize overnight news, earnings and my positions" using the finance tools, delivered to a chat or channel.
- Chat Controls pane "Markets": active connection, symbol context, timeframe, so the model knows what chart you are looking at.
- Tax: lot-level ledger and wash-sale flagging (stocks now, crypto pending H.R. 10357) exported as CSV.

### 4.8 Frontend (`/markets`)
- Dashboard: equity, P&L, positions, open orders, bot health.
- Chart: Lightweight Charts with indicator overlays, trade markers from backtests or live fills, "explain this" button that sends the visible window to the model.
- Strategies, Backtests, Bots, Connections pages.
- Sidebar entry gated like Automations/Calendar.

## 5. Phasing and effort

Effort assumes one developer working with an AI pair, and counts calendar days of focused work.

| Phase | Scope | Est. |
|---|---|---|
| 0. Prove it with no code | Run Alpaca MCP (paper) and OpenBB MCP servers, add them as MCP tool servers in Admin, chat-trade on paper. | 0.5 day |
| 1. Foundation | Feature flag, permission, `finance_connection` table + migration, settings UI, Alpaca + CCXT + Massive adapters, bar cache, read-only built-in tools, basic quote/bars chart page. | 2 weeks |
| 2. Strategies + backtesting | Strategy table, editor, worker pool executor, vectorbt + backtesting.py runners, metrics, benchmark, results UI, `run_backtest` tool. | 2 to 3 weeks |
| 3. Paper execution | Bot runner, risk gate, order/fill ledger, per-tool approval, `place_order` on paper, dashboard, notifications. | 2 to 3 weeks |
| 4. Live + hardening | Live flag with explicit confirmation, reconciliation against broker state on restart, kill switch, tax ledger, tests. | 1 to 2 weeks |
| 5. Optional | freqtrade sidecar, IBKR via `ib_async`, nautilus_trader, walk-forward optimizer, FreqAI/RL experiments. | open |

Total for a usable paper-trading system (phases 1 to 3): roughly 6 to 8 weeks. Phase 0 is worth doing first regardless.

## 6. Risks and constraints

- **Performance claims.** Do not expect the model to pick winners. Treat every backtest as suspect until it beats buy-and-hold out of sample with realistic costs and slippage.
- **Look-ahead bias.** Models have memorized price history. Never ask the model "what happens next" on historical data as a signal; only use it on live, post-cutoff information, and keep backtests strategy-driven.
- **Latency.** This architecture is for bar-close strategies (1 minute and slower). It is not a high-frequency platform.
- **Single process.** Long backtests must not run inside the FastAPI event loop. Use the process pool from day one.
- **Credentials.** Reuse the Fernet valve encryption; never log keys; keep live keys in separate connections from paper keys.
- **Licences.** backtesting.py is AGPL, vectorbt has the Commons Clause, Lightweight Charts requires attribution, freqtrade is GPL (sidecar keeps it separate). All fine for personal self-hosting; revisit if the app is ever distributed.
- **Regulatory (US).** The pattern day trader rule ended June 4 2026, but brokers may phase in until Oct 2027. Wash-sale tracking is needed for stocks and likely soon for crypto. Crypto venue availability is state-dependent (NY: Coinbase/Gemini only). Free data tiers are personal-use licences; keep the deployment single-user.
- **Broker ops burden.** IBKR needs a gateway with daily restarts and weekly 2FA. Schwab needs a weekly manual login. Alpaca and Tradier do not, which is why they are first.

## 7. Decisions (answered 2026-10-12)

1. **State: Massachusetts.** Coinbase, Gemini and Kraken are all available. Check Alpaca crypto eligibility in the Alpaca dashboard when crypto is picked up.
2. **Broker: Alpaca** (paper first, then live). No second broker for now.
3. **Asset classes: US stocks/ETFs only.** Options and crypto are deferred, but the design keeps hooks for them:
   - `asset_class` column (`us_equity` | `option` | `crypto`) on connections, orders, positions and strategies; only `us_equity` is enabled.
   - Broker adapter protocol takes an `asset_class` and an order model that can later carry multiple legs (`legs: list[OrderLeg]`, one leg for stocks).
   - `DataProvider` protocol has `option_chain()` and crypto symbol handling stubs that raise `NotImplementedError`.
   - Provider registry so a CCXT/Coinbase adapter drops in without schema changes.
4. **Strategy authoring: both.** A constrained template (indicators, entry/exit rules, sizing, stops as JSON) for model-generated strategies, which compiles to the same Python `Strategy` interface, plus plain Python for hand-written or advanced strategies. Templates are the default for "Ask the model"; raw Python needs explicit opt-in per strategy. Both run only in the worker process pool.
5. **freqtrade sidecar: deferred** with crypto. Everything runs in-process (plus the worker pool) for now.
6. **Data: Alpaca only.** Alpaca's free plan covers stock bars and quotes (IEX real-time, multi-year history), a market-data websocket, and news. Fundamentals come from SEC EDGAR, which is free and needs no account (only a User-Agent with an email). Massive, FMP and Tiingo stay optional adapters behind the same protocol and are not built in Phase 1.
7. **Phase 2 backtest engine: built-in, not vectorbt (for now).** A small deterministic engine (`utils/finance/backtest/engine.py`, pandas/numpy only) avoids the numba dependency and the Commons Clause / AGPL licences. It is long-only, fills at the next bar's open, models commission, slippage, stop-loss and take-profit, splits cash equally across symbols, and runs in a spawned process pool with a timeout (`FINANCE_BACKTEST_WORKERS`, `FINANCE_BACKTEST_TIMEOUT`). Strategies record an `engine` column so vectorbt can be added later for heavy sweeps. Python strategies are admin-only because they run arbitrary code on the server.

## Appendix A: provider details

### Brokers (US stocks/ETFs)
| Broker | Paper | Auth | Python SDK | Verdict |
|---|---|---|---|---|
| Alpaca | Yes, full mirror of live, separate keys | Static key+secret | `alpaca-py` 0.44 (Aug 2026), maintained | **Primary.** Stocks, options, crypto in one account; free IEX real-time websocket (30 symbols, 200 req/min); $99/mo for full SIP feed. Crypto only in ~25 states (not NY/TX/FL/NJ/PA). |
| Interactive Brokers | Yes (separate login) | Running TWS/IB Gateway; daily restart + weekly 2FA re-auth | `ib_async` (community, active) | Best breadth, highest ops burden. Later/optional. |
| Tradier | Sandbox (15-min delayed) | Personal token, never expires | None official | Free, simple; options L1 only. Secondary. |
| Public.com | Not documented | Secret key → short-lived token | Official `publicdotcom-py` + MCP server (Apr 2026) | Free stocks/options/crypto; personal-use terms. Secondary. |
| Schwab | **No** | OAuth2, refresh token dies weekly, manual browser login | `schwab-py` | Only if you already hold assets there. |
| Webull | Unverified | App key + IP allowlist + 2FA | Official SDK | Requires application approval. Skip initially. |
| Robinhood | None | API key + Ed25519 | — | Official API is **crypto-only**; stock wrappers violate ToS. Skip. |

### Crypto exchanges
| Exchange | Sandbox | Notes |
|---|---|---|
| Coinbase Advanced Trade | Mocked/static only | NY-licensed, official `coinbase-advanced-py`, 10k req/hr. **Primary crypto venue.** |
| Kraken | Futures demo only; spot UAT on request | Not for NY/ME residents. |
| Gemini | Full sandbox | NY-licensed; use via CCXT. Good dry-run venue. |
| Binance.US | No testnet | Excludes NY, TX and ~14 other states. |
| CCXT 4.5.x | `set_sandbox_mode()` routes to exchange testnets where they exist | One abstraction over all of the above; no built-in paper simulator. |

### Market data
| Provider | Free tier | Paid tier worth considering |
|---|---|---|
| Alpaca Data | IEX real-time quotes/bars/websocket, crypto bars | $99/mo Algo Trader Plus (full SIP, unlimited symbols) |
| Massive (ex-Polygon) | 5 calls/min, EOD, 2y | Starter $29 (unlimited calls, 15-min delayed, 5y, websocket) |
| Tiingo | 50 req/hr, EOD + IEX + crypto + news | Power $30/mo |
| Financial Modeling Prep | 250 calls/day, fundamentals | Starter $29/mo |
| SEC EDGAR | Free, 10 req/s, needs User-Agent with email | — (`edgartools`) |
| Databento | $125 starter credits | Usage-based; tick/L2 history |
| CoinGecko | 10k calls/mo | Analyst $129 |
| Alpha Vantage | 25 req/day | Weak for intraday; skip |
| yfinance | Unofficial scraper, heavy 429 blocking in 2026 | Research-only, never in the bot's data path |

News/social: NewsAPI free tier is 24h-delayed (useless for signals); Reddit self-service app registration closed late 2025; X API has no free tier. Tiingo news and FMP news are the practical cheap options.

### Legal and compliance (US, personal use)
- **Pattern Day Trader rule is gone** as of June 4 2026 (FINRA Rule 4210 amendments), replaced by intraday margin standards. Brokers may phase in until Oct 2027, so some still enforce the old $25k floor. Cash accounts still face settlement rules.
- **Wash sales** apply to stocks/ETFs; a churning bot will generate them, so track tax lots. Crypto is currently exempt but H.R. 10357 would extend wash-sale rules to crypto (cleared House Ways & Means Sept 2026). Form 1099-DA crypto reporting is in effect.
- **Broker terms**: Alpaca, IBKR, Tradier, Public, Webull, Schwab all allow self-directed automation on your own account. Free data tiers are personal/non-redistribution licences: keep the deployment single-user.
- **Crypto by state**: NY needs BitLicense venues (Coinbase, Gemini). Kraken excludes NY/ME, Binance.US excludes NY/TX and others, Alpaca crypto covers ~25 states.

## Appendix B: framework and project details

### Backtest / execution engines
| Framework | 2026 status | Python | Live | Licence | Fit |
|---|---|---|---|---|---|
| vectorbt OSS | 1.1.2 (Oct 9 2026), optional Rust engine | 3.11 to 3.14 | No | Apache + Commons Clause | Primary backtest engine |
| backtesting.py | 0.6.6 (Jul 2026) | >=3.9 | No | AGPL-3.0 | Simple `Strategy` subclass; single instrument |
| nautilus_trader | 1.231 stable, 2.0 RC | >=3.12 | Yes, many venues | LGPL-3.0 | Phase-3 upgrade path |
| freqtrade | 2026.9, monthly | >=3.11 | Yes (CCXT) | GPL-3.0 | Crypto sidecar via REST |
| hummingbot | 2.16 | >=3.10 | Yes | Apache-2.0 | Market making; heavy |
| jesse | 3.3.2 | ? | Paid plugin | MIT core | Skip |
| backtrader | Last release 2023 | old | stale | GPL-3.0 | Skip |
| zipline-reloaded | 3.1.1 (Jul 2025) | 3.10 to 3.13 | No | Apache-2.0 | Skip (heavy ingest) |
| QuantConnect LEAN | Active | 3.11 in Docker | Yes | Apache-2.0 | Skip (paid CLI tier, .NET) |

### AI-trading open source worth borrowing from
- **TradingAgents** (Apache-2.0, ~110k stars, v0.6 Oct 2026): parallel analysts, bull/bear debate, risk team, PM. Borrow the pipeline shape as a prompt/agent pattern using this app's subagent delegation, do not embed (LangGraph dependency overlap).
- **ai-hedge-fund** (MIT): investor-persona agents plus backtester. Good persona prompts.
- **FinRobot** (Apache-2.0): "models reason, software computes" principle.
- **FinRL** (MIT): RL agents, research-grade. Phase 5 at most.
- **hummingbot/condor** (MIT): LLM agent + MCP tools driving bots, a useful reference architecture.

### MCP servers (connect via Admin > Tool Servers today)
- Alpaca MCP server (official, MIT): trading, data, account, news; `ALPACA_PAPER_TRADE=true` default; streamable-http transport.
- OpenBB MCP server 2.0.1 (official, Apache-2.0): 100+ data providers as tools.
- Massive MCP, CoinGecko MCP (official), financial-datasets MCP (stale since mid 2025).

### Charts
- Lightweight Charts v5.2.1: Apache-2.0 with mandatory attribution; multi-pane, series markers, toolkit plugin. `svelte-lightweight-charts` wrapper is pre-v5 and stale.
- Apache ECharts: candlestick + markLine, no attribution; better for analytics dashboards.
- Plotly: keep for Jupyter research plots only.

### Research references
- The Alpha Illusion (arXiv:2605.16895, May 2026): reported agent alpha does not survive temporal controls; recommends LLM as auditable information interface with separate risk/execution modules.
- Look-Ahead-Bench (arXiv:2601.13770, Jan 2026): open-weight models leak memorized history even with point-in-time data.
- StockBench (arXiv:2510.02209): most LLM agents fail to beat buy-and-hold post-cutoff.
- Agent Market Arena (arXiv:2510.11695): framework matters more than backbone; results not reproducible across windows.
