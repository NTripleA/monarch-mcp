# AGENTS.md

This file provides guidance and project context for AI coding agents working on `monarch-mcp`, following the open [AGENTS.md](https://agents.md) specification.

## Project Overview

`monarch-mcp` is a Model Context Protocol (MCP) server that provides personal finance integration with Monarch Money. It exposes tools, resources, and prompt templates to AI agents over standard I/O (stdio) using the FastMCP framework from the MCP Python SDK.

- **Primary Entry Point:** `server.py`
- **Helper Modules:** `browser_auth.py` (loopback browser cookie capture)
- **Language / Runtime:** Python 3.10+ (managed via `uv`)
- **Package Name (PyPI):** `monarch-mcp-jamiew`
- **MCP Registry ID:** `io.github.jamiew/monarch-mcp`

---

## PII & Financial Data Privacy

> [!CAUTION]
> **CRITICAL**: Never commit or include personally identifiable financial data in code, documentation, tests, or commit messages.

This includes:
- Real account names (e.g., specific credit card names like "Main Credit Card")
- Real merchant names from the user's transaction history
- Real transaction IDs, category IDs, or account IDs from Monarch Money
- Real dollar amounts tied to specific transactions
- Any data that could identify the user's financial institutions or spending habits

**Always use generic, obviously-fake examples:** `"Main Credit Card"`, `"Corner Deli"`, `"cat_001"`, `"txn_123"`. Brand names like `"Starbucks"` are acceptable as generic illustrative examples in docstrings, but real user data must never be committed.

---

## Setup & Development Commands

This project uses `uv` for dependency management and virtual environments.

### Environment & Basic Operations
```bash
# Install dependencies and create/update virtual environment
uv sync

# Run the MCP server directly for testing (logs to stderr)
uv run python server.py

# Add or remove dependencies
uv add <package>
uv remove <package>
```

### Testing, Linting & Validation
```bash
# Run all automated tests
uv run pytest tests/ -v --tb=short

# Run type checker (strict mode)
uv run mypy server.py

# Run linter
uv run ruff check .

# Check formatting (auto-fix with: uv run ruff format .)
uv run ruff format --check .

# Full pre-push check (mirrors CI: ruff check, ruff format, mypy, pytest)
uv run python scripts/ci.py
```

### Debugging & Server Execution
```bash
# Test server directly (all logs routed to stderr)
uv run python server.py

# Force fresh login bypass session cache
MONARCH_FORCE_LOGIN=true uv run python server.py
```

### Troubleshooting & Common Issues
- **Session expired:** Delete `~/.monarch-mcp/session.pickle` or run with `MONARCH_FORCE_LOGIN=true`.
- **JSON parse errors:** All stdout is suppressed with `contextlib.redirect_stdout()` to protect JSON-RPC framing.
- **MCP protocol compliance:** Logging and warnings are routed strictly to `stderr`; third-party library output is suppressed.
- **AsyncIO errors:** Server uses `run_stdio_async()` in async context.
- **SSL warnings:** Suppressed from `gql.transport.aiohttp` to prevent stdout contamination.
- **Date serialization errors:** `build_date_filter()` returns ISO strings for JSON safety.
- **Broken pipe errors:** Server implements graceful shutdown and I/O error recovery.
- **Date parsing failures:** Enhanced with multi-format fallbacks and descriptive error messages.

### Usage Analytics & Monitoring

The server decorates tools with `@track_usage`, logging metrics to `stderr`. In MCP clients (e.g., Claude Code, Cursor, Windsurf, or Claude Desktop), filter client logs or stderr streams:

```bash
# Monitor all analytics (tool calls, performance, errors)
tail -f <mcp-client-log> | grep "\[ANALYTICS\]"

# Watch for optimization suggestions
tail -f <mcp-client-log> | grep "\[OPTIMIZATION\]"

# Monitor performance (slow operations > 1 second)
tail -f <mcp-client-log> | grep "\[ANALYTICS\]" | grep -E "time: [1-9][0-9]*\.[0-9]+s"

# View session summaries and top tools
tail -f <mcp-client-log> | grep "session_summary"

# Debug tool calls with arguments
tail -f <mcp-client-log> | grep "\[TOOL_CALL\]"

# Monitor result sizes for context usage optimization (> 50 KB)
tail -f <mcp-client-log> | grep "\[RESULT_SIZE\]" | grep -E "[5-9][0-9]\.[0-9]+ KB|[0-9]{3,}\.[0-9]+ KB"
```

**Log Markers:**
- `[TOOL_CALL] get_transactions | args: {'limit': 100, 'start_date': 'last month', 'verbose': False}`
- `[ANALYTICS] tool_called: get_transactions | time: 0.234s | status: success`
- `[RESULT_SIZE] get_transactions | chars: 12,543 | size: 12.25 KB | transactions: 42 items`
- `[OPTIMIZATION] Consider using get_complete_financial_overview instead of separate get_accounts + get_transactions calls`
- `[ANALYTICS] session_summary: 15 calls | top_tool: get_transactions`

---

## Code Philosophy & Quality Standards

### Human-Centric Design Principles
- **Simplicity over complexity:** Prefer direct, straightforward solutions over elaborate abstractions.
- **Clean, self-documenting code:** Descriptive function and variable names tell the story.
- **Human-readable over clever:** Code should be immediately understandable upon inspection.
- **Minimal comments:** Code should explain what and why; avoid redundant comments.

### Type Safety (Zero Tolerance)
- **NO `Any` types:** Every value must have explicit, specific types.
- **NO `as` assertions:** Use runtime validation with Pydantic instead of type casting.
- **Explicit annotations:** Every function parameter and return value must be typed.
- **Union types:** Always accompany union types with proper type guards.

### Error Handling
- **Specific exceptions:** Never catch bare or generic `Exception`. Catch concrete error types.
- **Structured logging:** Use `structlog` to emit context-rich logs for debugging.
- **Fail fast:** Validate parameters and payloads early; fail clearly with descriptive messages.
- **Graceful degradation:** Handle expected failures (e.g. individual API call failures in batch endpoints) without crashing the server.

---

## Architecture & Design Patterns

### Modern FastMCP Implementation
- Built with `FastMCP` from `mcp.server.fastmcp` (latest MCP protocol specification).
- Tools are declared cleanly using `@mcp.tool()` decorators.
- Transport: JSON-RPC 2.0 over stdio.
- Automatic capability negotiation and tool discovery.
- Tools, resources, and prompts include human-friendly `title` metadata.
- Context progress reporting via injected `Context` (`ctx.report_progress`) on batch/long-running tools.

### Structured Output Models
- Every tool returns a typed Pydantic model (`outputSchema` + structured content with a text fallback for older clients).
- Monarch GraphQL responses are dicts (e.g. `{"accounts": [...]}`), not bare lists. Use `extract_list(response, key)` (or `extract_transactions_list`) to unwrap the inner list before counting or mapping it.
- `convert_dates_to_strings()` guarantees clean JSON serialization.

### Tool Coverage (22 Tools, 5 Resources, 4 Prompts)
- **Core:** `get_accounts`, `get_transactions`, `get_budgets`, `get_cashflow`
- **Categories:** `get_transaction_categories`
- **Transactions:** `create_transaction`, `update_transaction`, `update_transactions_bulk`, `search_transactions`
- **Splits:** `get_transaction_splits`, `update_transaction_splits` (full-replace; empty list removes all splits)
- **Investments:** `get_account_holdings` (requires `account_id`), `get_account_history`
- **Banking:** `get_institutions`, `refresh_accounts`
- **Planning:** `get_recurring_transactions`, `set_budget_amount`
- **Manual Accounts:** `create_manual_account`
- **Auth:** `authenticate_browser_session` (browser sign-in via MCP `elicit_url`; captures cookies via loopback server)
- **Batch Operations:** `get_spending_summary`, `update_transactions_bulk`
- **Intelligent Analysis:** `get_complete_financial_overview`, `analyze_spending_patterns`
- **Resources:** 3 static lists + 2 parameterized templates (`accounts://{account_id}/holdings|history`).
- **Prompts:** 4 guided prompt templates with argument completions.

### Authentication & Session Management
Three authentication paths are evaluated in order by `initialize_client()`:
1. **Token Auth:** `MONARCH_TOKEN` containing the value after `Token ` in the GraphQL `Authorization` header.
2. **Cookie Auth (Recommended / Confirmed Working):** `MONARCH_COOKIES` containing the full browser `Cookie` header string (must include Cloudflare clearance cookies `cf_clearance` and `__cf_bm`, not just `session_id` + `csrftoken`). The required `X-Csrftoken` header is set automatically by `set_cookies()`. Cookies are a one-time seed and persist to the session file.
3. **Password + MFA:** `MONARCH_EMAIL`, `MONARCH_PASSWORD`, and `MONARCH_MFA_SECRET`.
   - *Note on CAPTCHA:* Monarch CAPTCHA-gates programmatic password logins. Incorrectly reported TOTP errors (`404` or `429 CAPTCHA_REQUIRED`) are detected by `is_captcha_error()` to immediately abort rather than escalating the block.
   - TOTP codes are single-use per 30-second window; `seconds_until_retry()` waits for the next window before retrying.

- **Browser Auth Tool:** `authenticate_browser_session` runs a single-use loopback server, requests the client to open it via `Context.elicit_url` (fallback: `webbrowser.open`), auto-detects cookies via `rookiepy` (optional `browser` extra) or accepts manual paste, then calls `authenticate_with_cookies()`. Auth errors reference `BROWSER_AUTH_HINT` so agents can recover interactively.
- **Session Files:** Persisted in `~/.monarch-mcp/` (override with `MONARCH_SESSION_DIR`) with strict permissions (0700 directory, 0600 file).
- **Environment Loading:** `.env` is loaded only by `load_env_file()` at server entry points, never at module import time, preventing test side effects. System environment variables take precedence over `.env`. Integration tests require `MONARCH_RUN_INTEGRATION=1`.

### Key Design Patterns
1. **Single Client Instance:** Global `mm_client` maintains a single authenticated MonarchMoney connection.
2. **Session Persistence:** Session state is cached to prevent frequent re-authentication.
3. **Type-Safe Error Handling:** All exceptions are typed and handled gracefully.
4. **Runtime Validation:** External data is parsed and validated through Pydantic models.
5. **Strict Protocol Compliance:** JSON-RPC 2.0 over stdio with stdout suppression and stderr logging.

---

## Development & Git Workflow

### Development Process
When working autonomously or picking up tasks:
1. **Read Current Status:** Check the remaining tasks and status sections in this `AGENTS.md` file.
2. **Select Next Task:** Pick the highest-priority pending task.
3. **Implement & Test:** Code following type-safety and human-centric standards.
4. **Validate Before Commit:** Run `uv run python scripts/ci.py` to ensure all checks pass.
5. **Atomic Commits:** Make focused commits for individual features, fixes, or optimizations.
6. **Update Status:** Periodically update `AGENTS.md` status and task lists for major milestones.

### Pre-Push CI Check
Always run:
```bash
uv run python scripts/ci.py
```
This executes `ruff check`, `ruff format --check`, `mypy`, and `pytest`, mirroring the GitHub Actions CI matrix (Python 3.10–3.13).

### Git Commit Conventions
Format:
```
<type>: <concise description>

<optional body explaining why and what changed>
```

**Allowed Types:**
- `feat`: New feature or tool implementation
- `fix`: Bug fix or error resolution
- `perf`: Performance optimization
- `refactor`: Code restructuring without behavior changes
- `test`: Adding or improving tests
- `docs`: Documentation updates
- `chore`: Maintenance, dependency updates, tooling

### Releasing & Publishing
- Published to PyPI as `monarch-mcp-jamiew` and to MCP Registry as `io.github.jamiew/monarch-mcp`.
- Users install via `uvx monarch-mcp-jamiew`.
- **Critical:** The `[project.scripts]` entry point `monarch-mcp-jamiew = "server:run"` MUST remain a **synchronous** wrapper (`run()`), never the async `main()` directly, or `uvx` launches a coroutine that is never awaited.
- Release workflow: `/release` bumps `pyproject.toml`, tags `vX.Y.Z`, and creates a GitHub release. GitHub Actions (`.github/workflows/publish.yml`) publishes via OIDC trusted publishing (no static tokens stored).

### AGENTS.md Maintenance
Update this file when:
- Major milestones or features are completed (move items from REMAINING to COMPLETED).
- Architectural changes, new dependencies, or new tools/resources are introduced.
- Quality metrics change (test count, tool count).
- Note: Keep `CLAUDE.md` in sync if both files are maintained in the repository.

---

## Upstream Library & Fork Landscape

**Lineage:** `monarch-mcp` wraps a community Python client for the Monarch Money API. The client is a fork of a fork:

| Repository | Role | Health / Notes |
|---|---|---|
| [`hammem/monarchmoney`](https://github.com/hammem/monarchmoney) | Original parent | Effectively abandoned (stale since Nov 2025). Lacks new domain fix and maintenance. **Do not depend on this.** |
| [`bradleyseanf/monarchmoneycommunity`](https://github.com/bradleyseanf/monarchmoneycommunity) | **Active fork in use** | Most active fork. Includes API domain updates (`api.monarch.com`), gql 4.0 fix, session persistence, cookie auth fallback, receipt upload. |
| [`keithah/monarchmoney-enhanced`](https://github.com/keithah/monarchmoney-enhanced) | Sibling fork (stale) | Stale since Jan 2026. Large feature surface (~126 methods), but lacks our fork's attachment/receipt methods. Cherry-pick GraphQL queries from it rather than switching. |

**Dependency Pin:**
- In `pyproject.toml` → `[tool.uv.sources]`, `monarchmoneycommunity` is pinned to a specific commit SHA (`c6904e4ec8938c7386e73ed503b7c0a0693dd6a4`) representing `dev` HEAD for reproducible builds.
- When updating, update the commit SHA and comment in `pyproject.toml`.

**Unused Capabilities in Current Fork (Quick Tool Wins):**
- Transaction tags (`get/set/create_transaction_tag`)
- `find_duplicate_transactions`
- `get_transaction_details`
- `get_cashflow_summary`
- `get_subscription_details`
- `get_credit_history`
- `delete_transaction`
- `create_transaction_category`
- `update_account`
- `request_accounts_refresh_and_wait`
- `upload_receipt_to_inbox` (Monarch AI receipt matching)

**Candidate Capabilities to Cherry-Pick from `keithah/monarchmoney-enhanced`:**
1. **Rules engine:** `create_transaction_rule`, `preview_transaction_rule`, `apply_rules_to_existing_transactions`, `get/update/delete_transaction_rule` (`transaction_service.py`).
2. **Net worth & Insights:** `get_net_worth_history`, `get_insights`, `get_investment_performance`, `get_credit_score`.
3. **Goals & Bills:** `get_goals`, `create_goal`, `get_bills`.

---

## Status, Metrics & Implementation Roadmap

### Production Quality Metrics
- **Tests:** 206+ passing tests across unit, integration, and mock suites.
- **Tools:** 22 typed tools returning Pydantic models (structured output).
- **Resources:** 3 static resources + 2 parameterized templates (`accounts://{account_id}/holdings|history`).
- **Prompts:** 4 prompt templates with argument completions.
- **Type Checking:** 100% clean under strict MyPy configuration.
- **Formatting / Linting:** 100% clean under Ruff.

### Completed Features
- **Phase 1 (Critical):** Zero `Any` types, strict typing, FastMCP migration, security permissions (0700/0600), structlog logging, full core API coverage.
- **Phase 2 (Advanced Features):** Bulk transaction updates (`update_transactions_bulk`), natural language date parsing, smart spending aggregations (`get_spending_summary`), `@track_usage` analytics, financial overview (`get_complete_financial_overview`), spending pattern forecasting (`analyze_spending_patterns`).
- **Phase 3 (Production Stability):** JSON-RPC stdio protocol compliance, stdout suppression, third-party log silencing, session recovery, comprehensive testing.
- **Phase 4a (Critical Resilience):** Date serialization fixes (ISO strings), broken pipe handlers, dependency updates, browser auth loopback (`authenticate_browser_session`).

### Remaining Priorities

#### 🔄 High Priority
1. **Enhanced Error Handling & Resilience:**
   - Retry logic with exponential backoff for transient network failures.
   - Circuit breaker pattern for API rate limits.
   - Distinct exception classes for specific Monarch Money API errors.
   - Standardized MCP-compliant error responses with error codes.
2. **Advanced Session Management:**
   - Per-request session validation.
   - Proactive session refresh prior to expiry.
   - Atomic file operations for session cache writes.
   - Session health monitoring and recovery.
3. **Data Caching & Performance:**
   - In-memory caching for low-churn data (accounts, categories).
   - Optional Redis backend for multi-instance deployments (`uv add redis`).
   - Cache invalidation strategies with TTL.
   - Connection pooling for Monarch Money API requests.

#### 🔄 Medium Priority
4. **Observability & Monitoring:**
   - OpenTelemetry metrics export (`uv add opentelemetry-api`).
   - MCP health check tool.
   - Correlation IDs for request tracing across tool chains.
5. **Financial Intelligence:**
   - Spending predictions and anomaly detection.
   - Transaction auto-categorization and rule simulation.
   - Budget vs. actual variance analysis.
   - Investment performance tracking.
6. **Advanced Tool Features:**
   - Bulk transaction import/export.
   - Fuzzy transaction searching.
   - Automated recurring bill detection.
   - Savings goals and target tracking.

#### 🔄 Low Priority
7. **Code Organization:**
   - Modular breakdown into `auth.py`, `tools.py`, `models.py`, and `config.py` (current single-file `server.py` remains manageable but can be split if needed).
   - Pydantic Settings configuration management.
8. **Developer Experience:**
   - Automated API documentation generation from tool schemas.
   - Development server mode with hot-reloading.

---

## Documentation & Protocol References

- **MCP Protocol Specification:** [https://modelcontextprotocol.io/llms-full.txt](https://modelcontextprotocol.io/llms-full.txt)
- **MCP Python SDK:** [https://github.com/modelcontextprotocol/python-sdk](https://github.com/modelcontextprotocol/python-sdk)
- **FastMCP Documentation:** [https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/fastmcp.md](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/fastmcp.md)
- **Monarch Money Community Fork:** [https://github.com/bradleyseanf/monarchmoneycommunity](https://github.com/bradleyseanf/monarchmoneycommunity)
- **MCP Servers Examples:** [https://github.com/modelcontextprotocol/servers](https://github.com/modelcontextprotocol/servers)
- **Current MCP Protocol Target:** 2025-11-25 stable specification. Server uses structured tool output (`outputSchema`), resource templates, completions, and `Context` progress reporting.
