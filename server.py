#!/usr/bin/env python3
"""MonarchMoney MCP Server - Provides access to Monarch Money financial data via MCP protocol."""

import argparse
import asyncio
import contextlib
import functools
import io
import json
import logging
import os
import pickle
import re
import signal
import stat
import sys
import time
import uuid
import warnings
import webbrowser
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal, ParamSpec, TypeVar

import aiohttp
import structlog
from dateutil import parser as date_parser
from dateutil.relativedelta import relativedelta
from gql.transport.exceptions import TransportClosed, TransportConnectionFailed, TransportServerError
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ResourceError, ToolError
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.types import (
    Completion,
    CompletionArgument,
    CompletionContext,
    ContentBlock,
    PromptReference,
    ResourceTemplateReference,
    ToolAnnotations,
)
from mcp.types import Tool as MCPTool
from monarchmoney import CaptchaRequiredException, MonarchMoney, RequireMFAException
from pydantic import (
    AnyUrl,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictStr,
    ValidationError,
    field_validator,
)
from structlog.typing import EventDict, WrappedLogger

import browser_auth

# Type definitions for Monarch Money API responses
JsonSerializable = str | int | float | bool | None | list["JsonSerializable"] | dict[str, "JsonSerializable"]

# Reusable tool annotations — all tools are closed-world (only talk to Monarch Money API).
# Annotations are advisory hints for clients; MONARCH_ENABLE_WRITES is the enforcement.
READONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
# Adds a new record; never modifies an existing one.
WRITE_CREATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
# Overwrites fields of existing financial records (repeating the same call has no further effect).
WRITE_REPLACE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)
# Asks Monarch to re-sync institutions; changes no stored record directly.
WRITE_REFRESH = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
# Replaces the locally stored Monarch session.
SESSION_REPLACE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)

# Every tool that changes Monarch data. Gated by MONARCH_ENABLE_WRITES.
WRITE_TOOLS = frozenset(
    {
        "create_transaction",
        "update_transaction",
        "update_transactions_bulk",
        "update_transaction_splits",
        "set_budget_amount",
        "create_manual_account",
        "refresh_accounts",
    }
)

# Tools that only make sense next to the user's own browser; never served over HTTP.
LOCAL_ONLY_TOOLS = frozenset({"authenticate_browser_session"})

Transport = Literal["stdio", "http"]


@dataclass
class RuntimeConfig:
    """Process-wide transport and safety settings, set once by the entry point."""

    transport: Transport = "stdio"
    writes_enabled: bool = True
    max_bulk_updates: int = 25


DEFAULT_MAX_BULK_UPDATES = 25
MAX_BULK_UPDATES_CEILING = 100

RUNTIME = RuntimeConfig()

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def parse_bool_env(name: str, default: bool) -> bool:
    """Read a boolean env var strictly; an unrecognised value is a startup error."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ValueError(f"{name} must be true or false, got an unrecognised value")


def parse_max_bulk_updates() -> int:
    raw = os.getenv("MONARCH_MAX_BULK_UPDATES")
    if raw is None or raw.strip() == "":
        return DEFAULT_MAX_BULK_UPDATES
    try:
        value = int(raw.strip())
    except ValueError as e:
        raise ValueError("MONARCH_MAX_BULK_UPDATES must be an integer") from e
    if not 1 <= value <= MAX_BULK_UPDATES_CEILING:
        raise ValueError(f"MONARCH_MAX_BULK_UPDATES must be between 1 and {MAX_BULK_UPDATES_CEILING}")
    return value


def tool_disabled_reason(name: str) -> str | None:
    """Why a tool is unavailable in the current runtime, or None when it may run."""
    if name in LOCAL_ONLY_TOOLS and RUNTIME.transport != "stdio":
        return f"{name} is only available when the server runs locally over stdio."
    if name in WRITE_TOOLS and not RUNTIME.writes_enabled:
        return f"{name} is disabled: this server runs with MONARCH_ENABLE_WRITES=false (read-only)."
    return None


class ToolDisabledError(ValueError):
    """A gated tool was invoked while its gate is closed."""


def require_tool_enabled(name: str) -> None:
    """In-function guard; the dispatcher enforces the same rule before the tool runs."""
    reason = tool_disabled_reason(name)
    if reason is not None:
        raise ToolDisabledError(reason)


def parse_flexible_date(date_input: str) -> date:
    """
    Parse flexible date inputs including natural language with comprehensive error handling.

    Supports:
    - "today", "now"
    - "yesterday"
    - "this month", "current month"
    - "last month", "previous month"
    - "this year", "current year"
    - "last year", "previous year"
    - "last week", "this week"
    - "30 days ago", "6 months ago"
    - Any date format supported by dateutil.parser
    """
    if not date_input:
        raise ValueError("Date input cannot be empty")

    # Handle common natural language patterns
    date_input = date_input.lower().strip()
    today = date.today()

    if date_input in ["today", "now"]:
        return today
    elif date_input == "yesterday":
        return today - timedelta(days=1)
    elif date_input in ["this month", "current month"]:
        return date(today.year, today.month, 1)
    elif date_input in ["last month", "previous month"]:
        # Handle month rollover correctly
        if today.month == 1:
            return date(today.year - 1, 12, 1)
        else:
            return date(today.year, today.month - 1, 1)
    elif date_input in ["this year", "current year"]:
        return date(today.year, 1, 1)
    elif date_input in ["last year", "previous year"]:
        return date(today.year - 1, 1, 1)
    elif date_input == "last week":
        return today - timedelta(days=7)
    elif date_input == "this week":
        # Start of this week (Monday)
        days_since_monday = today.weekday()
        return today - timedelta(days=days_since_monday)

    # Handle relative patterns like "30 days ago", "6 months ago"
    relative_pattern = re.match(r"(\d+)\s+(days?|weeks?|months?|years?)\s+ago", date_input)
    if relative_pattern:
        amount = int(relative_pattern.group(1))
        unit = relative_pattern.group(2).rstrip("s")  # Remove plural 's'

        try:
            if unit == "day":
                return today - timedelta(days=amount)
            elif unit == "week":
                return today - timedelta(weeks=amount)
            elif unit == "month":
                result = today - relativedelta(months=amount)
                return result.date() if hasattr(result, "date") else result
            elif unit == "year":
                result = today - relativedelta(years=amount)
                return result.date() if hasattr(result, "date") else result
        except (ValueError, OverflowError) as e:
            log.warning("Invalid relative date calculation", input=date_input, amount=amount, unit=unit, error=str(e))
            raise ValueError(f"Invalid relative date: {date_input}") from e

    # Try parsing with dateutil for standard date formats
    try:
        parsed_datetime = date_parser.parse(date_input)
        parsed_date = parsed_datetime.date()

        # Validate reasonable date range (1900 to 50 years in future)
        min_date = date(1900, 1, 1)
        max_date = date(today.year + 50, 12, 31)

        if parsed_date < min_date or parsed_date > max_date:
            log.warning("Date outside reasonable range", input=date_input, parsed_date=parsed_date.isoformat())
            raise ValueError(f"Date {parsed_date.isoformat()} is outside reasonable range (1900-{today.year + 50})")

        return parsed_date

    except (ValueError, TypeError, OverflowError) as e:
        log.warning("Failed to parse date with dateutil", input=date_input, error=str(e))

        # Provide helpful error message with suggestions
        suggestions = [
            "Try formats like: 2024-01-15, Jan 15 2024, 15/01/2024",
            "Or natural language: today, yesterday, last month, this year",
            "Or relative: 30 days ago, 6 months ago, 1 year ago",
        ]
        suggestion_text = ". ".join(suggestions)
        raise ValueError(f"Could not parse date '{date_input}'. {suggestion_text}") from e


def parse_iso_date(value: str, field: str) -> str:
    """Strictly validate a YYYY-MM-DD date for a write and return it as an ISO string."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except (TypeError, ValueError) as e:
        raise ValueError(f"Invalid date format for {field}. Use YYYY-MM-DD (e.g., 2024-01-15).") from e


def build_date_filter(start_date: str | None, end_date: str | None) -> dict[str, str]:
    """
    Build date filter dictionary with flexible parsing and comprehensive error recovery.

    Args:
        start_date: Start date string (flexible format supported)
        end_date: End date string (flexible format supported)

    Returns:
        Dictionary with ISO format date strings

    Raises:
        ValueError: If date parsing fails completely after all fallback attempts

    Note:
        Monarch Money API requires BOTH start_date AND end_date when filtering by date.
        If only one is provided, the other will be auto-filled with a sensible default:
        - Missing end_date: defaults to today
        - Missing start_date: defaults to start of current month
    """
    filters: dict[str, str] = {}

    # Auto-fill missing dates for better UX (Monarch API requires both or neither)
    if start_date and not end_date:
        # User provided start but not end - default end to today
        end_date = "today"
        log.info("Auto-filling missing end_date with 'today'", start_date=start_date)
    elif end_date and not start_date:
        # User provided end but not start - need to parse end_date first to choose smart default
        # If end_date is in the past, use beginning of that month; otherwise use this month
        try:
            parsed_end = parse_flexible_date(end_date)
            today = date.today()

            # If end date is in the past or in a different month, use first of that month
            if parsed_end < today or parsed_end.month != today.month or parsed_end.year != today.year:
                # Use first day of the end_date's month
                start_date = date(parsed_end.year, parsed_end.month, 1).isoformat()
                log.info(
                    "Auto-filling missing start_date with first of end_date's month",
                    end_date=end_date,
                    calculated_start=start_date,
                )
            else:
                # End date is this month, use "this month"
                start_date = "this month"
                log.info("Auto-filling missing start_date with 'this month'", end_date=end_date)
        except ValueError:
            # If we can't parse end_date yet, just use "this month" and let validation catch issues later
            start_date = "this month"
            log.info("Auto-filling missing start_date with 'this month' (end_date parse pending)", end_date=end_date)

    # parse_flexible_date already handles all formats (natural language, ISO, dateutil)
    if start_date:
        parsed_date = parse_flexible_date(start_date)
        filters["start_date"] = parsed_date.isoformat()
        log.info("Parsed start_date", input=start_date, parsed=parsed_date.isoformat())

    if end_date:
        parsed_date = parse_flexible_date(end_date)
        filters["end_date"] = parsed_date.isoformat()
        log.info("Parsed end_date", input=end_date, parsed=parsed_date.isoformat())

    # Validate date range logic
    if "start_date" in filters and "end_date" in filters:
        start = date.fromisoformat(filters["start_date"])
        end = date.fromisoformat(filters["end_date"])

        if start > end:
            log.warning("Start date is after end date", start_date=filters["start_date"], end_date=filters["end_date"])
            raise ValueError(f"Start date ({filters['start_date']}) cannot be after end date ({filters['end_date']})")

    return filters


def convert_dates_to_strings(obj: Any) -> Any:
    """
    Recursively convert all date/datetime objects to ISO format strings.

    This ensures that the data can be serialized by any JSON encoder,
    not just our custom one. This is necessary because the MCP framework
    may attempt to serialize the response before we can use our custom encoder.
    """
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    elif isinstance(obj, dict):
        return {key: convert_dates_to_strings(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return [convert_dates_to_strings(item) for item in obj]
    elif isinstance(obj, tuple):
        return tuple(convert_dates_to_strings(item) for item in obj)
    else:
        return obj


def extract_transactions_list(response: Any) -> list[dict[str, Any]]:
    """
    Extract the transactions list from monarchmoney API response.

    The monarchmoney library returns:
    {
        "allTransactions": {
            "totalCount": 123,
            "results": [...]  # <-- actual transactions
        },
        "transactionRules": ...
    }

    This function extracts the results list from the nested structure.
    """
    if isinstance(response, list):
        # Already a list (shouldn't happen with current API)
        return response
    elif isinstance(response, dict):
        # Check for the nested structure
        if "allTransactions" in response:
            all_txns = response["allTransactions"]
            if isinstance(all_txns, dict) and "results" in all_txns:
                results = all_txns["results"]
                if isinstance(results, list):
                    return results
        # Fallback: maybe it's a different structure
        log.warning("Unexpected transaction response structure", keys=list(response.keys()))
        return []
    else:
        log.error("Unexpected transaction response type", response_type=str(type(response)))
        return []


def extract_list(response: Any, key: str) -> list[Any]:
    """Pull a named list out of a Monarch API response.

    Most Monarch GraphQL queries return a dict like ``{"accounts": [...]}`` rather
    than a bare list, so the inner list has to be unwrapped before counting it.
    Tolerates an already-flat list and unexpected shapes (returns []).
    """
    if isinstance(response, list):
        return response
    if isinstance(response, dict):
        value = response.get(key)
        if isinstance(value, list):
            return value
    return []


def format_transactions_compact(transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Format transactions in a compact format with only essential fields.

    Returns simplified transaction objects with only:
    - id, date, amount
    - merchant name, plaidName (original statement name)
    - category id + name (id needed for updates)
    - account display name
    - needsReview flag
    - pending flag (only if True)
    - notes (only if present)

    Use verbose=True to get full transaction details when needed.
    """
    compact: list[dict[str, Any]] = []

    for txn in transactions:
        if not isinstance(txn, dict):
            continue

        category = txn.get("category")
        compact_txn: dict[str, Any] = {
            "id": txn.get("id"),
            "date": txn.get("date"),
            "amount": txn.get("amount"),
            "merchant": txn.get("merchant", {}).get("name") if isinstance(txn.get("merchant"), dict) else None,
            "plaidName": txn.get("plaidName"),
            "category": category.get("name") if isinstance(category, dict) else None,
            "categoryId": category.get("id") if isinstance(category, dict) else None,
            "account": txn.get("account", {}).get("displayName") if isinstance(txn.get("account"), dict) else None,
            "needsReview": txn.get("needsReview", False),
        }

        # Only include pending if actually pending (saves bytes on the common case)
        if txn.get("pending"):
            compact_txn["pending"] = True

        # Include notes if present
        if txn.get("notes"):
            compact_txn["notes"] = txn.get("notes")

        compact.append(compact_txn)

    return compact


def _build_transaction_filters(
    start_date: str | None,
    end_date: str | None,
    account_id: str | None = None,
    category_id: str | None = None,
    tag_ids: str | None = None,
    has_attachments: bool | None = None,
    has_notes: bool | None = None,
    hidden_from_reports: bool | None = None,
    is_split: bool | None = None,
    is_recurring: bool | None = None,
) -> dict[str, Any]:
    """Build filters dict for get_transactions API calls.

    Shared by get_transactions and search_transactions to avoid duplication.
    """
    filters: dict[str, Any] = build_date_filter(start_date, end_date)

    # monarchmoney expects account_ids and category_ids as LISTS
    if account_id:
        filters["account_ids"] = [account_id]
    if category_id:
        filters["category_ids"] = [category_id]
    if tag_ids:
        filters["tag_ids"] = [t.strip() for t in tag_ids.split(",")]

    # Boolean filters (only include if explicitly set)
    if has_attachments is not None:
        filters["has_attachments"] = has_attachments
    if has_notes is not None:
        filters["has_notes"] = has_notes
    if hidden_from_reports is not None:
        filters["hidden_from_reports"] = hidden_from_reports
    if is_split is not None:
        filters["is_split"] = is_split
    if is_recurring is not None:
        filters["is_recurring"] = is_recurring

    return filters


# Configure logger to output to stderr only with error handling
class SafeStreamHandler(logging.StreamHandler[Any]):
    """Stream handler that gracefully handles broken pipes."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            super().emit(record)
        except (BrokenPipeError, ConnectionResetError):
            # Silently ignore broken pipe errors during logging
            pass
        except Exception:
            # Let other logging errors bubble up
            self.handleError(record)


class ThirdPartyLogSanitizer(logging.Filter):
    """Keep library log records (mcp, uvicorn, aiohttp, ...) free of data and credentials.

    Our own records are already redacted by the structlog processor. Library records
    can embed request details or exception text (a resource URI with an account id,
    an upstream error echoing a payload), so their tracebacks are reduced to the
    exception type and their messages are scrubbed of credentials.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name in ("server", "__main__"):
            return True
        if record.exc_info and record.exc_info[1] is not None:
            # The accompanying message often names the failing request (e.g. a resource
            # URI carrying an account id), so keep only where and what kind of failure.
            message = f"exception in {record.name} [{type(record.exc_info[1]).__name__}]"
        else:
            message = scrub_text(record.getMessage())
        record.msg = message
        record.args = None
        record.exc_info = None
        record.exc_text = None
        return True


_log_handler = SafeStreamHandler(sys.stderr)
_log_handler.addFilter(ThirdPartyLogSanitizer())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[_log_handler],
)

# Redaction of credentials from every log line. Applied as a structlog processor so it
# covers all existing call sites and any added later. Temporary TOTP codes are not
# redacted -- they expire in 30s and are needed for MFA debugging.
_REDACTED = "<redacted>"

_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "csrftoken",
        "email",
        "key",
        "passwd",
        "password",
        "secret",
        "session_id",
        "token",
        "username",
    }
)

_SENSITIVE_SUFFIXES = ("_email", "_key", "_password", "_secret", "_token", "_username")

# Financial data never belongs in logs. These keys are redacted whatever the value type
# (amounts are floats), as a backstop behind call sites that simply do not log them.
_FINANCIAL_KEYS = frozenset(
    {
        "account",
        "account_id",
        "account_ids",
        "account_name",
        "amount",
        "args",
        "balance",
        "category",
        "category_id",
        "kwargs",
        "merchant",
        "merchant_name",
        "net_amount",
        "notes",
        "query",
        "search",
        "splits",
        "transaction_id",
        "updates",
    }
)

_EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Credential shapes that can surface inside free text such as exception messages.
_CREDENTIAL_PATTERNS = (
    re.compile(r"(?i)\b(session_id|csrftoken|cf_clearance|__cf_bm|_cfuvid|sessionid)=[^;\s,'\"]+"),
    re.compile(r"(?i)\b(cookie|authorization|x-csrftoken)(['\"]?\s*[:=]\s*)[^\n,}]+"),
    re.compile(r"(?i)\btoken\s+[A-Za-z0-9._\-]{8,}"),
)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in _SENSITIVE_KEYS or lowered.endswith(_SENSITIVE_SUFFIXES)


def _is_financial_key(key: str) -> bool:
    return key.lower() in _FINANCIAL_KEYS


def scrub_text(text: str) -> str:
    """Mask emails and credential-looking fragments in free text."""
    text = _EMAIL_PATTERN.sub(_REDACTED, text)
    text = _CREDENTIAL_PATTERNS[0].sub(lambda m: f"{m.group(1)}={_REDACTED}", text)
    text = _CREDENTIAL_PATTERNS[1].sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", text)
    return _CREDENTIAL_PATTERNS[2].sub(f"Token {_REDACTED}", text)


def _redact_value(key: str, value: JsonSerializable) -> JsonSerializable:
    if _is_financial_key(key) and value is not None:
        return _REDACTED
    if _is_sensitive_key(key) and isinstance(value, str):
        return _REDACTED
    return _scrub(value)


def _scrub(value: JsonSerializable) -> JsonSerializable:
    """Mask credentials and emails embedded in free-text values (error strings, messages)."""
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {k: _redact_value(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


def redact_sensitive(_logger: WrappedLogger, _method_name: str, event_dict: EventDict) -> EventDict:
    """structlog processor: drop credentials and financial fields before anything is rendered."""
    return {key: _redact_value(key, value) for key, value in event_dict.items()}


# Configure structured logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        redact_sensitive,
        structlog.processors.JSONRenderer(),
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

# Get structured logger for this module
log = structlog.get_logger(__name__)

# Suppress third-party library logging to reduce noise
logging.getLogger("aiohttp").setLevel(logging.ERROR)
logging.getLogger("monarchmoney").setLevel(logging.ERROR)
logging.getLogger("gql").setLevel(logging.ERROR)
logging.getLogger("gql.transport").setLevel(logging.ERROR)

warnings.filterwarnings("ignore", category=UserWarning, module="gql.transport.aiohttp")

# Session tracking for usage analytics. Holds timings and sizes only -- never arguments
# or results -- and is bounded so a long-running remote server cannot grow without limit.
current_session_id = str(uuid.uuid4())
USAGE_HISTORY_PER_TOOL = 200
usage_patterns: dict[str, deque[dict[str, str | float]]] = {}

P = ParamSpec("P")
R = TypeVar("R")

_COUNTED_RESULT_FIELDS = ("transactions", "accounts", "categories", "results", "splits")


def result_count(result: object) -> int | None:
    """Coarse item count for a tool result, for logging. Never inspects item contents."""
    if not isinstance(result, BaseModel):
        return None
    for field in _COUNTED_RESULT_FIELDS:
        value = getattr(result, field, None)
        if isinstance(value, list):
            return len(value)
    return None


def http_status_of(error: BaseException) -> int | None:
    """HTTP status carried by a library exception, if any."""
    for attr in ("status", "code"):
        value = getattr(error, attr, None)
        if isinstance(value, int) and 100 <= value <= 599:
            return value
    return None


def error_category(error: BaseException) -> str:
    """Coarse, content-free classification of an exception for logs."""
    if isinstance(error, ToolDisabledError):
        return "disabled"
    if isinstance(error, WriteOutcomeUnknownError):
        return "write_outcome_unknown"
    if isinstance(error, SessionUnavailableError):
        return "session_unavailable"
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return "timeout"
    if isinstance(error, Exception) and is_captcha_error(error):
        return "captcha"
    if isinstance(error, Exception) and is_auth_error(error):
        return "auth"
    if isinstance(error, (aiohttp.ClientError, ConnectionError, TransportClosed, TransportConnectionFailed)):
        return "network"
    if isinstance(error, (ValueError, ValidationError, TypeError)):
        return "validation"
    return "upstream"


def safe_error_fields(error: BaseException) -> dict[str, str | int]:
    """Log fields describing an error without its message, which may carry financial data."""
    fields: dict[str, str | int] = {"error_type": type(error).__name__, "error_category": error_category(error)}
    status = http_status_of(error)
    if status is not None:
        fields["http_status"] = status
    return fields


def _record_usage(tool_name: str, status: str, execution_time: float, result_size: int) -> None:
    history = usage_patterns.setdefault(tool_name, deque(maxlen=USAGE_HISTORY_PER_TOOL))
    history.append(
        {
            "session_id": current_session_id,
            "tool_name": tool_name,
            "timestamp": time.time(),
            "status": status,
            "execution_time": execution_time,
            "result_size": result_size,
        }
    )


def track_usage(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Log tool name, outcome, timing, and result size -- never arguments or payloads."""

    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        start_time = time.time()
        tool_name = func.__name__

        log.info("tool_call", tool=tool_name, arg_names=sorted(k for k in kwargs if k != "ctx"))

        try:
            result = await func(*args, **kwargs)
        except Exception as e:
            execution_time = time.time() - start_time
            _record_usage(tool_name, "error", execution_time, 0)
            log.error("tool_error", tool=tool_name, time_s=round(execution_time, 3), **safe_error_fields(e))
            raise

        execution_time = time.time() - start_time
        # Tools return Pydantic models; serialize to JSON for an accurate wire-size measurement.
        payload = result.model_dump_json() if isinstance(result, BaseModel) else str(result or "")
        result_chars = len(payload)
        _record_usage(tool_name, "success", execution_time, result_chars)

        log.info(
            "tool_success",
            tool=tool_name,
            time_s=round(execution_time, 3),
            result_chars=result_chars,
            result_kb=round(result_chars / 1024, 2),
            result_count=result_count(result),
        )
        return result

    return wrapper


class MonarchFastMCP(FastMCP):
    """FastMCP with the server's safety gates applied at the protocol layer.

    - Disabled tools (writes when MONARCH_ENABLE_WRITES=false, local-only tools over
      HTTP) are left out of tools/list AND refused by tools/call, so a client holding a
      cached tool list still cannot invoke them.
    - Write tools reject argument names outside their schema: FastMCP otherwise drops
      unknown arguments silently, turning a mistyped field into a no-op "success".
    - Error text returned to clients is scrubbed of credentials.
    """

    async def list_tools(self) -> list[MCPTool]:
        tools = await super().list_tools()
        return [tool for tool in tools if tool_disabled_reason(tool.name) is None]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Sequence[ContentBlock] | dict[str, Any]:
        reason = tool_disabled_reason(name)
        if reason is not None:
            log.warning("tool_call_rejected", tool=name, reason="disabled")
            raise ToolError(reason)

        if name in WRITE_TOOLS:
            tool = self._tool_manager.get_tool(name)
            if tool is not None:
                allowed = set(tool.parameters.get("properties", {}))
                unknown = sorted(set(arguments) - allowed)
                if unknown:
                    log.warning("tool_call_rejected", tool=name, reason="unknown_arguments")
                    raise ToolError(
                        f"{name} rejected unknown argument(s): {', '.join(unknown)}. "
                        f"Allowed: {', '.join(sorted(allowed))}. Nothing was changed."
                    )

        try:
            return await super().call_tool(name, arguments)
        except Exception as e:
            raise ToolError(scrub_text(str(e))) from e

    async def read_resource(self, uri: AnyUrl | str) -> Iterable[ReadResourceContents]:
        try:
            return await super().read_resource(uri)
        except Exception as e:
            raise ResourceError(scrub_text(str(e))) from e


# Initialize the FastMCP server
mcp = MonarchFastMCP("monarch-money")


# =============================================================================
# Structured output models
#
# Each tool returns a typed model so FastMCP emits an ``outputSchema`` and
# machine-readable structured content (plus a text fallback for older clients).
# Monarch's GraphQL payloads are deep and evolve, so passthrough fields are typed
# as ``JsonValue`` (recursive JSON, not ``Any``) and ``MMModel`` allows unknown
# extra keys to flow through. Shapes we construct ourselves are modeled precisely.
# =============================================================================


class MMModel(BaseModel):
    """Base for response models — tolerates extra upstream fields."""

    model_config = ConfigDict(extra="allow")


class AccountsResult(MMModel):
    accounts: list[JsonValue]
    count: int


class TransactionsResult(MMModel):
    transactions: list[JsonValue]
    count: int
    verbose: bool


class SearchMetadata(BaseModel):
    query: str
    result_count: int
    filters_applied: dict[str, JsonValue]


class SearchResult(MMModel):
    search_metadata: SearchMetadata
    transactions: list[JsonValue]


class BudgetsResult(MMModel):
    budgets: JsonValue
    message: str | None = None


class CashflowResult(MMModel):
    cashflow: JsonValue


class CategoriesResult(MMModel):
    categories: list[JsonValue]
    count: int
    verbose: bool


class TransactionResult(MMModel):
    transaction: JsonValue


class TransactionSplit(BaseModel):
    """One leg of a split transaction.

    The split amounts must sum to the parent transaction's amount (Monarch
    validates this and rejects the update otherwise). Amounts keep the parent's
    sign convention — expenses are negative, income positive.
    """

    model_config = ConfigDict(extra="forbid")

    amount: float
    category_id: str | None = None
    merchant_name: str | None = None
    notes: str | None = None


class TransactionSplitsResult(MMModel):
    transaction_id: str
    has_split_transactions: bool
    splits: list[JsonValue]


class UpdateSplitsResult(MMModel):
    transaction_id: str
    has_split_transactions: bool
    splits: list[JsonValue]
    message: str


class BulkSummary(BaseModel):
    total: int
    succeeded: int
    failed: int
    skipped: int = 0


class BulkItemResult(BaseModel):
    transaction_id: str | None = None
    status: str
    error: str | None = None


class TransactionUpdate(BaseModel):
    """One transaction update, validated in full before anything is sent to Monarch.

    Unknown fields and loosely-typed values (a string "false" for a flag, a boolean for
    an amount) are rejected rather than coerced into a different write.
    """

    model_config = ConfigDict(extra="forbid")

    transaction_id: StrictStr = Field(min_length=1)
    amount: float | None = None
    merchant_name: StrictStr | None = None
    category_id: StrictStr | None = None
    date: StrictStr | None = None
    notes: StrictStr | None = None
    goal_id: StrictStr | None = None
    hide_from_reports: StrictBool | None = None
    needs_review: StrictBool | None = None

    @field_validator("amount", mode="before")
    @classmethod
    def _amount_is_a_number(cls, value: object) -> object:
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError("amount must be a number")
        return value

    @field_validator("date")
    @classmethod
    def _date_is_iso(cls, value: str | None) -> str | None:
        return None if value is None else parse_iso_date(value, "date")


BULK_UPDATE_CONCURRENCY = 5


def build_transaction_update(update: TransactionUpdate) -> dict[str, Any]:
    """Keyword arguments for MonarchMoney.update_transaction; rejects an empty update."""
    fields = update.model_dump(exclude_none=True)
    if len(fields) == 1:
        raise ValueError(f"No fields to update for transaction {update.transaction_id}. Nothing was changed.")
    return fields


def parse_bulk_updates(raw: str) -> list[TransactionUpdate]:
    """Validate a whole bulk-update batch before any of it is sent to Monarch."""
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in updates parameter: {e.msg}. Nothing was changed.") from e

    if not isinstance(items, list):
        raise ValueError("updates parameter must be a JSON array of transaction updates")

    limit = RUNTIME.max_bulk_updates
    if len(items) > limit:
        raise ValueError(
            f"Batch has {len(items)} updates but the limit is {limit} (MONARCH_MAX_BULK_UPDATES). "
            "Split it into smaller batches. Nothing was changed."
        )

    parsed: list[TransactionUpdate] = []
    problems: list[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            problems.append(f"item {index}: must be an object")
            continue
        try:
            update = TransactionUpdate.model_validate(item)
        except ValidationError as e:
            details = "; ".join(f"{'.'.join(str(p) for p in err['loc']) or 'item'}: {err['msg']}" for err in e.errors())
            problems.append(f"item {index}: {details}")
            continue
        if len(update.model_dump(exclude_none=True)) == 1:
            problems.append(f"item {index}: no fields to update")
            continue
        parsed.append(update)

    seen: set[str] = set()
    duplicates: set[str] = set()
    for update in parsed:
        if update.transaction_id in seen:
            duplicates.add(update.transaction_id)
        seen.add(update.transaction_id)
    if duplicates:
        problems.append(f"transaction_id repeated in batch: {', '.join(sorted(duplicates))}")

    if problems:
        raise ValueError("Bulk update rejected; nothing was changed. " + " | ".join(problems))
    return parsed


class BulkUpdateResult(MMModel):
    summary: BulkSummary
    results: list[BulkItemResult]
    message: str | None = None


class HoldingsResult(MMModel):
    holdings: JsonValue


class AccountHistoryResult(MMModel):
    account_id: str
    history: JsonValue


class InstitutionsResult(MMModel):
    # Monarch's institution-settings query returns a dict (credentials, accounts,
    # subscription), not a flat list, so the full payload is passed through.
    institutions: JsonValue


class RecurringResult(MMModel):
    recurring: JsonValue


class SetBudgetResult(MMModel):
    category_id: str
    amount: float
    result: JsonValue


class CreateAccountResult(MMModel):
    account: JsonValue


class RefreshResult(MMModel):
    requested: bool
    account_count: int
    result: JsonValue = None


class BrowserAuthResult(MMModel):
    """Outcome of a browser sign-in. Deliberately carries no credential fields --
    captured cookies must never reach structured output or the model context."""

    authenticated: bool
    method: str
    message: str


class Totals(BaseModel):
    income: float
    expenses: float
    net: float


class GroupSummary(BaseModel):
    income: float
    expenses: float
    net: float
    count: int


class Period(BaseModel):
    start: str | None = None
    end: str | None = None


class SpendingSummaryResult(MMModel):
    period: Period
    group_by: str
    groups: dict[str, GroupSummary]
    totals: Totals


class FinancialOverview(MMModel):
    period: str
    accounts: JsonValue = None
    budgets: JsonValue = None
    cashflow: JsonValue = None
    transactions: JsonValue = None
    categories: JsonValue = None
    transaction_summary: JsonValue = None
    batch_metadata: JsonValue = None


class SpendingPatterns(MMModel):
    analysis_period: JsonValue = None
    monthly_trends: JsonValue = None
    category_analysis: JsonValue = None
    account_usage: JsonValue = None
    budget_performance: JsonValue = None
    forecast: JsonValue = None
    metadata: JsonValue = None


# =============================================================================
# MCP Resources - Read-only data endpoints for reference data
# =============================================================================


@mcp.resource("categories://list", title="Transaction Categories")
async def list_categories_resource() -> str:
    """
    List all transaction categories available in Monarch Money.

    Returns a JSON array of category objects with id, name, group, and icon.
    This is read-only reference data useful for understanding available categories
    before creating or updating transactions.
    """
    await ensure_authenticated()
    categories = await api_call_with_retry("get_transaction_categories")
    return json.dumps(convert_dates_to_strings(categories), indent=2)


@mcp.resource("accounts://list", title="Linked Accounts")
async def list_accounts_resource() -> str:
    """
    List all linked financial accounts in Monarch Money.

    Returns a JSON array of account objects including checking, savings,
    credit cards, investments, and other account types with their balances
    and institution information.
    """
    await ensure_authenticated()
    accounts = await api_call_with_retry("get_accounts")
    return json.dumps(convert_dates_to_strings(accounts), indent=2)


@mcp.resource("institutions://list", title="Linked Institutions")
async def list_institutions_resource() -> str:
    """
    List all connected financial institutions in Monarch Money.

    Returns a JSON array of institution objects showing which banks,
    brokerages, and other financial institutions are connected to the account.
    """
    await ensure_authenticated()
    institutions = await api_call_with_retry("get_institutions")
    return json.dumps(convert_dates_to_strings(institutions), indent=2)


@mcp.resource("accounts://{account_id}/holdings", title="Account Holdings")
async def account_holdings_resource(account_id: str) -> str:
    """
    Investment holdings for a specific account (resource template).

    The ``account_id`` path segment selects which account's portfolio to return.
    Mirrors the ``get_account_holdings`` tool but as an addressable resource.
    """
    await ensure_authenticated()
    holdings = await api_call_with_retry("get_account_holdings", account_id=account_id)
    return json.dumps(convert_dates_to_strings(holdings), indent=2)


@mcp.resource("accounts://{account_id}/history", title="Account Balance History")
async def account_history_resource(account_id: str) -> str:
    """
    Historical balance data for a specific account (resource template).

    The ``account_id`` path segment selects which account's balance history to
    return. Mirrors the ``get_account_history`` tool but as an addressable resource.
    """
    await ensure_authenticated()
    history = await api_call_with_retry("get_account_history", account_id=account_id)
    return json.dumps(convert_dates_to_strings(history), indent=2)


# =============================================================================
# MCP Prompts - Reusable prompt templates for common financial analyses
# =============================================================================


@mcp.prompt(title="Analyze Spending")
def analyze_spending(period: str = "this month", category: str | None = None) -> str:
    """
    Generate a prompt template for analyzing spending patterns.

    Args:
        period: Time period to analyze (e.g., "this month", "last 3 months", "2024")
        category: Optional category to focus on (e.g., "Food & Dining", "Shopping")
    """
    category_focus = f" specifically for {category}" if category else ""
    return f"""Please analyze my spending{category_focus} for {period}.

Use the get_transactions tool to fetch transaction data for the specified period, then provide:

1. **Total Spending**: Sum of all expenses
2. **Top Categories**: Which categories had the most spending
3. **Trends**: Any notable patterns or changes
4. **Insights**: Specific observations about spending habits
5. **Recommendations**: Actionable suggestions to optimize spending

Focus on practical insights rather than just listing numbers."""


@mcp.prompt(title="Budget Review")
def budget_review(month: str = "current") -> str:
    """
    Generate a prompt template for reviewing budget performance.

    Args:
        month: Which month to review ("current", "last", or "YYYY-MM" format)
    """
    return f"""Please review my budget performance for {month}.

Use get_budgets and get_transactions tools to compare budgeted amounts vs actual spending:

1. **Budget vs Actual**: For each category, show budgeted amount, actual spending, and variance
2. **Over Budget**: Highlight categories where spending exceeded budget
3. **Under Budget**: Show categories with unused budget
4. **Overall Status**: Am I on track for the month?
5. **Adjustments**: Suggest any budget adjustments based on actual patterns

Present the data in a clear, easy-to-scan format."""


@mcp.prompt(title="Financial Health Check")
def financial_health_check() -> str:
    """
    Generate a comprehensive financial health assessment prompt.

    This prompt guides a thorough review of accounts, spending, and budgets.
    """
    return """Please perform a comprehensive financial health check.

Use the available tools to gather data and provide:

1. **Account Overview**:
   - Total assets and liabilities
   - Net worth calculation
   - Account balances summary

2. **Cash Flow Analysis**:
   - Monthly income vs expenses
   - Savings rate
   - Recurring transactions review

3. **Spending Analysis**:
   - Top spending categories (last 30 days)
   - Unusual or large transactions
   - Comparison to previous month

4. **Budget Status**:
   - Categories on track vs off track
   - Projected month-end status

5. **Action Items**:
   - Specific recommendations
   - Areas needing attention
   - Positive trends to maintain

Be concise but thorough. Highlight the most important insights first."""


@mcp.prompt(title="Categorize a Transaction")
def transaction_categorization_help(description: str) -> str:
    """
    Generate a prompt to help categorize a transaction.

    Args:
        description: The transaction description or merchant name
    """
    return f"""Help me categorize this transaction: "{description}"

First, use the categories://list resource to see all available categories.

Then suggest:
1. **Best Category Match**: The most appropriate category for this transaction
2. **Alternative Options**: Other categories that might fit
3. **Reasoning**: Why you recommend this categorization

If this is a merchant I transact with frequently, also note if the categorization
should be applied to future transactions from the same merchant."""


# =============================================================================
# MCP Completions - Argument autocompletion for prompts and resource templates
# =============================================================================


async def _category_name_completions(partial: str) -> list[str]:
    """Live category names for autocompletion. Best-effort: never raises."""
    try:
        await ensure_authenticated()
        categories = extract_list(await api_call_with_retry("get_transaction_categories"), "categories")
    except Exception as e:
        log.warning("completion_categories_failed", **safe_error_fields(e))
        return []
    names = [c.get("name", "") for c in categories if isinstance(c, dict) and c.get("name")]
    needle = partial.lower()
    return [n for n in names if needle in n.lower()][:100]


async def _account_id_completions(partial: str) -> list[str]:
    """Live account IDs for autocompletion. Best-effort: never raises."""
    try:
        await ensure_authenticated()
        accounts = extract_list(await api_call_with_retry("get_accounts"), "accounts")
    except Exception as e:
        log.warning("completion_accounts_failed", **safe_error_fields(e))
        return []
    ids = [a.get("id", "") for a in accounts if isinstance(a, dict) and a.get("id")]
    needle = partial.lower()
    return [i for i in ids if needle in i.lower()][:100]


@mcp.completion()
async def handle_completion(
    ref: PromptReference | ResourceTemplateReference,
    argument: CompletionArgument,
    context: CompletionContext | None,
) -> Completion | None:
    """Autocomplete prompt/resource-template arguments from live Monarch data.

    - prompt ``category`` argument -> transaction category names
    - resource-template ``account_id`` argument -> account IDs
    """
    if isinstance(ref, PromptReference) and argument.name == "category":
        return Completion(values=await _category_name_completions(argument.value), hasMore=False)

    if isinstance(ref, ResourceTemplateReference) and argument.name == "account_id":
        return Completion(values=await _account_id_completions(argument.value), hasMore=False)

    return None


class AuthState(Enum):
    """Track authentication state to prevent duplicate initialization attempts."""

    NOT_INITIALIZED = "not_initialized"
    INITIALIZING = "initializing"
    AUTHENTICATED = "authenticated"
    FAILED = "failed"


class SessionUnavailableError(ValueError):
    """No usable Monarch session, and no way to obtain one without the user."""


class SessionFileError(ValueError):
    """The session file is missing, unsafe, or not in the expected format."""


class WriteOutcomeUnknownError(ValueError):
    """A write failed in a way that does not tell us whether Monarch applied it."""


# Global variables for authentication
mm_client: MonarchMoney | None = None
auth_state: AuthState = AuthState.NOT_INITIALIZED
auth_lock: asyncio.Lock | None = None  # Created in async context
auth_error: str | None = None  # Store last auth error for debugging
auth_failed_at: float | None = None  # Timestamp of last auth failure for cooldown
AUTH_RETRY_COOLDOWN_SECONDS = 60  # Wait 60 seconds before retrying after FAILED state
# Session-only mode retries only once the session file itself changes (a fresh copy was
# provisioned), so it never hammers Monarch with a session that is known to be dead.
loaded_session_mtime: int | None = None  # mtime_ns of the session file the client came from
failed_session_mtime: int | None = None  # mtime_ns of the session file when auth last failed
last_failure_category: str | None = None

# Secure session directory with proper permissions.
# Resolve to an absolute, writable path: many MCP clients (e.g. Claude Desktop)
# launch the server with a read-only working directory like "/", so a relative
# ".mm" would fail with "Read-only file system". Honor MONARCH_SESSION_DIR if set,
# otherwise default to ~/.monarch-mcp which is always writable.
_session_dir_env = os.getenv("MONARCH_SESSION_DIR")
session_dir = Path(_session_dir_env).expanduser() if _session_dir_env else Path.home() / ".monarch-mcp"
session_file = session_dir / "session.pickle"

SESSION_FILE_MAX_BYTES = 64 * 1024


def _owned_by_us(st: os.stat_result) -> bool:
    return not hasattr(os, "getuid") or st.st_uid == os.getuid()


def secure_session_dir(path: Path) -> None:
    """Create the session directory 0700, tightening it if it is looser and ours."""
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = path.stat()
        if _owned_by_us(st) and stat.S_IMODE(st.st_mode) & 0o077:
            path.chmod(0o700)
            log.warning("session_dir_permissions_tightened", mode="0700")
        if not os.access(path, os.W_OK):
            log.warning("session_dir_not_writable")
    except OSError as e:
        log.warning("session_dir_unavailable", **safe_error_fields(e))


secure_session_dir(session_dir)


class _PrimitiveOnlyUnpickler(pickle.Unpickler):
    """Unpickler that refuses every class lookup.

    A Monarch session is a plain dict of strings. Dicts, lists, strings, and None are
    encoded with dedicated opcodes that never consult find_class, so refusing it here
    blocks every object-construction payload a tampered pickle could carry.
    """

    def find_class(self, module: str, name: str) -> type:
        raise pickle.UnpicklingError("session file references a Python object; refusing to load it")


@dataclass(frozen=True)
class StoredSession:
    token: str | None
    auth_mode: str
    cookies: dict[str, str] | None


_REQUIRED_SESSION_COOKIES = ("session_id", "csrftoken")


def _parse_session_payload(data: object) -> StoredSession:
    if not isinstance(data, dict) or not all(isinstance(k, str) for k in data):
        raise SessionFileError("session file is not a Monarch session")
    unexpected = set(data) - {"token", "auth_mode", "cookies"}
    if unexpected:
        raise SessionFileError("session file contains unexpected fields")

    token = data.get("token")
    if token is not None and (not isinstance(token, str) or not token):
        raise SessionFileError("session token has an invalid format")

    auth_mode = data.get("auth_mode", "token")
    if auth_mode not in ("token", "cookie"):
        raise SessionFileError("session auth mode is not recognised")

    cookies = data.get("cookies")
    if cookies is not None and (
        not isinstance(cookies, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in cookies.items())
    ):
        raise SessionFileError("session cookies have an invalid format")

    if auth_mode == "cookie" and cookies and all(name in cookies for name in _REQUIRED_SESSION_COOKIES):
        return StoredSession(token=token, auth_mode="cookie", cookies=cookies)
    if token:
        return StoredSession(token=token, auth_mode="token", cookies=None)
    raise SessionFileError("session file contains no usable credentials")


def read_session_file(path: Path) -> StoredSession:
    """Read and validate a session file exactly once, without executing any pickle code.

    Refuses symlinks, non-regular files, files others can write (a writable pickle is a
    code-execution vector), and oversized files; tightens a readable-by-others file we
    own to 0600.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError as e:
        raise SessionFileError("no session file is present") from e
    except OSError as e:
        raise SessionFileError(f"session file could not be opened ({type(e).__name__})") from e

    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise SessionFileError("session path is not a regular file")
        mode = stat.S_IMODE(st.st_mode)
        if mode & 0o022:
            raise SessionFileError("session file is writable by other users; fix with chmod 600")
        if mode & 0o044:
            if not _owned_by_us(st):
                raise SessionFileError("session file is readable by other users and not owned by this server")
            os.fchmod(fh.fileno(), 0o600)
            log.warning("session_file_permissions_tightened", mode="0600")
        raw = fh.read(SESSION_FILE_MAX_BYTES + 1)

    if len(raw) > SESSION_FILE_MAX_BYTES:
        raise SessionFileError("session file is too large to be a Monarch session")
    try:
        data = _PrimitiveOnlyUnpickler(io.BytesIO(raw)).load()
    except (pickle.UnpicklingError, EOFError, ValueError, IndexError, KeyError) as e:
        raise SessionFileError("session file is corrupt or not a Monarch session") from e
    return _parse_session_payload(data)


def client_from_session(stored: StoredSession) -> MonarchMoney:
    """Build a client from validated session data, mirroring MonarchMoney.load_session()."""
    client = MonarchMoney(token=stored.token) if stored.token else MonarchMoney()
    if stored.auth_mode == "cookie" and stored.cookies:
        client.set_cookies(stored.cookies)
    return client


def session_file_mtime() -> int | None:
    try:
        return session_file.stat().st_mtime_ns
    except OSError:
        return None


def load_client_from_session_file() -> MonarchMoney:
    """Replace mm_client with one built from the session file and record its mtime."""
    global mm_client, loaded_session_mtime
    mtime = session_file_mtime()
    client = client_from_session(read_session_file(session_file))
    mm_client = client
    loaded_session_mtime = mtime
    return client


def persist_session(client: MonarchMoney) -> None:
    """Write the client's session atomically with 0600 permissions.

    The library writes in place with the process umask; writing to a temp file in the
    same directory and renaming makes the swap atomic (including on a bind mount) and
    means a reader never sees a half-written or briefly world-readable file.
    """
    global loaded_session_mtime
    target = session_file
    tmp = target.parent / f".session.{uuid.uuid4().hex}.tmp"
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            client.save_session(str(tmp))
        if not tmp.exists():
            log.warning("session_not_persisted")
            return
        tmp.chmod(0o600)
        os.replace(tmp, target)
        loaded_session_mtime = session_file_mtime()
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


# Credential environment variables. HTTP mode is session-file only and ignores all of them.
CREDENTIAL_ENV_VARS = (
    "MONARCH_TOKEN",
    "MONARCH_COOKIES",
    "MONARCH_EMAIL",
    "MONARCH_PASSWORD",
    "MONARCH_MFA_SECRET",
    "MONARCH_FORCE_LOGIN",
)


def fallback_credentials() -> list[str]:
    """Login methods available when the saved session is missing or rejected.

    Empty in HTTP mode: the remote server only ever uses a pre-provisioned session file,
    so it can never attempt (and loop) a password/CAPTCHA login.
    """
    if RUNTIME.transport != "stdio":
        return []
    methods: list[str] = []
    if os.getenv("MONARCH_TOKEN"):
        methods.append("token")
    if os.getenv("MONARCH_COOKIES"):
        methods.append("cookies")
    if os.getenv("MONARCH_EMAIL") and os.getenv("MONARCH_PASSWORD"):
        methods.append("password")
    return methods


_AUTH_STATUS_PATTERN = re.compile(r"\b(401|403)\b")


def is_auth_error(error: Exception) -> bool:
    """Determine if an error is a genuine authentication/authorization failure.

    Only returns True for actual auth failures like 401, 403, invalid credentials.
    Does NOT treat library errors, connection issues, or other problems as auth failures.
    An HTTP status carried by the exception is authoritative when present.
    """
    status = http_status_of(error)
    if status is not None:
        return status in (401, 403)

    error_str = str(error).lower()

    # Exclude false positives first - these are NOT auth errors
    false_positives = [
        "connector",  # Library compatibility issue
        "aiohttp",  # Library issue
        "transport",  # Library issue
        "connection refused",  # Network issue, not auth
        "connection reset",  # Network issue, not auth
        "timeout",  # Network issue, not auth
    ]

    # Check for false positives first
    if any(fp in error_str for fp in false_positives):
        return False

    if _AUTH_STATUS_PATTERN.search(error_str):
        return True

    # Genuine authentication/authorization error indicators
    auth_indicators = [
        "unauthorized",
        "forbidden",
        "invalid credentials",
        "bad credentials",
        "authentication failed",
        "auth failed",  # Match "auth failed" messages
        "not authenticated",
        "authentication credentials were not provided",
        "invalid token",
        "token expired",
        "session expired",
        "session has expired",
    ]

    # Check for genuine auth errors
    return any(indicator in error_str for indicator in auth_indicators)


def clear_session(reason: str = "unknown") -> None:
    """Clear session files and reset authentication state to allow fresh re-authentication.

    This function performs a complete authentication reset:
    - Clears session files from disk (session.pickle, mm_session.pickle)
    - Resets the client instance (mm_client = None)
    - Resets auth state to NOT_INITIALIZED
    - Clears auth errors and failure timestamps

    Only called when fallback credentials can rebuild the session (stdio). A
    session-only server never deletes its provisioned session.

    Args:
        reason: Why the session is being cleared (for logging/debugging)
    """
    global mm_client, auth_state, auth_error, auth_failed_at

    log.info("auth_reset", reason=reason, previous_state=auth_state.value)

    if mm_client is not None:
        mm_client = None

    auth_state = AuthState.NOT_INITIALIZED
    auth_error = None
    auth_failed_at = None

    # Clear session files
    for path in [session_file, session_dir / "mm_session.pickle"]:
        if path.exists():
            try:
                path.unlink()
                log.info("session_file_cleared")
            except OSError as e:
                log.warning("session_file_clear_failed", **safe_error_fields(e))


def mark_auth_failed(message: str, category: str) -> None:
    global auth_state, auth_error, auth_failed_at, failed_session_mtime, last_failure_category
    auth_state = AuthState.FAILED
    auth_error = message
    auth_failed_at = time.time()
    failed_session_mtime = session_file_mtime()
    last_failure_category = category


async def _session_still_valid() -> bool:
    """One cheap read to confirm an auth-looking error really means the session is dead.

    Guards a session-only server against locking itself out on a misclassified error.
    """
    if mm_client is None:
        return False
    try:
        await mm_client.get_subscription_details()
    except Exception as e:
        return not is_auth_error(e)
    return True


async def _handle_session_only_auth_error(error: Exception, attempt: int) -> bool:
    """Session-only auth failure policy. Returns True when the call should be retried.

    Never deletes the provisioned session and never attempts a login. Retries once only
    when a freshly provisioned session file has appeared since the client was built.
    """
    current_mtime = session_file_mtime()
    if attempt == 0 and current_mtime is not None and current_mtime != loaded_session_mtime:
        try:
            load_client_from_session_file()
        except SessionFileError as e:
            message = f"The saved Monarch session could not be loaded: {e}. {auth_recovery_hint()}"
            mark_auth_failed(message, "session_unavailable")
            raise SessionUnavailableError(message) from e
        log.info("auth_session_reloaded_after_change")
        return True

    if await _session_still_valid():
        return False

    message = "Monarch rejected the saved session (it has most likely expired). " + auth_recovery_hint()
    mark_auth_failed(message, "auth")
    log.error("api_auth_failed_session_only", **safe_error_fields(error))
    raise SessionUnavailableError(message) from error


async def api_call_with_retry(method_name: str, *args: Any, max_retries: int = 3, **kwargs: Any) -> Any:
    """Wrapper for API calls that handles session expiration and retries.

    Only reacts to genuine auth errors; other errors (network, library issues, etc.) are
    raised immediately, never retried. An auth error means Monarch rejected the request,
    so retrying it -- even a write -- cannot apply it twice.

    - With fallback credentials (stdio): clear the session, re-authenticate, and retry
      up to ``max_retries`` times with exponential backoff.
    - Session-only (HTTP, or stdio without credentials): see
      ``_handle_session_only_auth_error``; no login is ever attempted.

    Raises:
        ValueError: If mm_client is not initialized
        SessionUnavailableError: The saved session was rejected and cannot be renewed here
        Exception: Re-raises any non-auth errors or auth errors after max_retries
    """
    global auth_state, mm_client

    last_error: Exception | None = None

    for attempt in range(max_retries + 1):  # +1 for initial attempt
        try:
            # Get the method from the current mm_client instance
            if mm_client is None:
                raise ValueError("mm_client is not initialized")

            method = getattr(mm_client, method_name)
            return await method(*args, **kwargs)

        except Exception as e:
            last_error = e

            if not is_auth_error(e):
                # Not an auth error - raise immediately without retry
                raise

            if not fallback_credentials():
                if await _handle_session_only_auth_error(e, attempt):
                    continue
                raise

            if attempt < max_retries:
                # Calculate exponential backoff delay (1s, 2s, 4s, ...)
                backoff_delay = 2**attempt
                log.warning(
                    "api_auth_error",
                    attempt=attempt + 1,
                    max_attempts=max_retries + 1,
                    backoff_s=backoff_delay,
                    **safe_error_fields(e),
                )

                # clear_session() will reset auth state and error automatically
                clear_session(reason=f"authentication failure during API call (attempt {attempt + 1})")

                # Wait before retry (exponential backoff)
                if backoff_delay > 0:
                    await asyncio.sleep(backoff_delay)

                # Re-authenticate
                await ensure_authenticated()
                log.info("api_retry_after_reauth", attempt=attempt + 2, max_attempts=max_retries + 1)

                # Continue to next iteration to retry with NEW mm_client
                continue

            # Max retries exhausted for auth error
            log.error("api_auth_retries_exhausted", max_retries=max_retries, **safe_error_fields(e))
            raise

    # Should never reach here, but handle it defensively
    if last_error:
        raise last_error
    raise RuntimeError("api_call_with_retry completed without result or error")


def is_ambiguous_write_failure(error: BaseException) -> bool:
    """A failure after which Monarch may or may not have applied the write.

    Timeouts, dropped connections, and 5xx responses can all happen after the server
    committed the mutation. Auth rejections and validation errors cannot.
    """
    if isinstance(error, (TimeoutError, asyncio.TimeoutError, ConnectionError)):
        return True
    if isinstance(
        error,
        (
            aiohttp.ServerDisconnectedError,
            aiohttp.ClientOSError,
            aiohttp.ClientPayloadError,
            TransportClosed,
            TransportConnectionFailed,
        ),
    ):
        return True
    if isinstance(error, TransportServerError):
        return error.code is None or error.code >= 500
    if isinstance(error, aiohttp.ClientResponseError):
        return error.status >= 500
    return False


WRITE_TIMEOUT_SECONDS = 30.0


async def write_call(
    method_name: str, *, creates: bool, description: str, max_auth_retries: int = 1, **kwargs: Any
) -> Any:
    """Run a Monarch mutation with write-safe failure handling.

    Never retries after an ambiguous failure (timeout, dropped connection, 5xx): the
    write may already have landed, and for creates a blind retry duplicates it. The
    only retry is a single one after an auth *rejection*, which proves nothing was
    applied. Ambiguous failures surface as WriteOutcomeUnknownError telling the client
    to check before retrying.
    """
    try:
        return await asyncio.wait_for(
            api_call_with_retry(method_name, max_retries=max_auth_retries, **kwargs), timeout=WRITE_TIMEOUT_SECONDS
        )
    except Exception as e:
        if not is_ambiguous_write_failure(e):
            raise
        log.error("write_outcome_unknown", method=method_name, **safe_error_fields(e))
        if creates:
            raise WriteOutcomeUnknownError(
                f"{description} may already have been created: the request to Monarch failed "
                f"({type(e).__name__}) after it was sent. Do NOT retry blindly -- first query "
                "(e.g. search_transactions / get_transactions / get_accounts) to check whether it "
                "exists, and only retry if it does not."
            ) from e
        raise WriteOutcomeUnknownError(
            f"{description} may or may not have been applied: the request to Monarch failed "
            f"({type(e).__name__}) after it was sent. Re-read the record to check its current "
            "state before sending the update again."
        ) from e


# A TOTP code is single-use and valid for one 30s window.
TOTP_PERIOD_SECONDS = 30

BROWSER_AUTH_HINT = "Call the authenticate_browser_session tool to sign in through your browser."

REPROVISION_HINT = (
    "This remote server only uses a pre-provisioned session and cannot sign in by itself. "
    "Re-create the session on your own computer (python server.py --provision-session), then "
    "copy the new session.pickle into the server's MONARCH_SESSION_DIR. No restart is needed; "
    "the server picks up the new file on the next call."
)


def auth_recovery_hint() -> str:
    return REPROVISION_HINT if RUNTIME.transport != "stdio" else BROWSER_AUTH_HINT


CAPTCHA_GUIDANCE = (
    "Monarch is requiring a CAPTCHA for programmatic login, so email/password/MFA "
    "authentication cannot succeed. Each further attempt escalates the block. "
    "Authenticate with a browser session instead. " + BROWSER_AUTH_HINT + " "
    "Alternatively set MONARCH_COOKIES to the full Cookie header from a logged-in "
    "app.monarch.com session (it must include session_id, csrftoken, and cf_clearance)."
)


def is_captcha_error(error: Exception) -> bool:
    """Detect Monarch's CAPTCHA gate.

    The library only raises CaptchaRequiredException on HTTP 403, but Monarch also
    signals the gate with HTTP 429 + error_code CAPTCHA_REQUIRED, which arrives as a
    generic LoginFailedException. Match on the message so both forms are caught.
    """
    return isinstance(error, CaptchaRequiredException) or "captcha" in str(error).lower()


def seconds_until_retry(default_delay: int, uses_mfa: bool) -> float:
    """How long to wait before retrying a failed login.

    A TOTP code is single-use within its 30-second window, so retrying sooner just
    replays a code Monarch has already consumed -- guaranteeing a second "invalid code"
    failure. When MFA is in play, wait for the next window so the retry gets a fresh code.
    """
    if not uses_mfa:
        return float(default_delay)
    return TOTP_PERIOD_SECONDS - (time.time() % TOTP_PERIOD_SECONDS) + 1.0


async def authenticate_with_token(token: str) -> None:
    """Authenticate with a Monarch API token copied from a browser session.

    Monarch's GraphQL API uses token authentication -- it rejects session cookies with
    "Authentication credentials were not provided" -- so this is the reliable way in when
    programmatic password login is CAPTCHA-gated. The token is taken from the
    Authorization header of any app.monarch.com GraphQL request.
    """
    global mm_client, auth_state, auth_error, auth_failed_at

    # MonarchMoney() sets the Authorization header only via its constructor; set_token()
    # alone would leave the header unset.
    mm_client = MonarchMoney(token=token)

    try:
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        with contextlib.redirect_stdout(stdout_capture), contextlib.redirect_stderr(stderr_capture):
            await mm_client.get_accounts()
        persist_session(mm_client)

        auth_state = AuthState.AUTHENTICATED
        auth_error = None
        log.info("auth_success", method="token")

    except Exception as e:
        error_msg = (
            f"Token authentication failed ({type(e).__name__}). Copy a fresh value from the "
            "Authorization header of a GraphQL request in a logged-in app.monarch.com session "
            "(the part after 'Token ')."
        )
        log.error("auth_token_failed", **safe_error_fields(e))
        mark_auth_failed(error_msg, error_category(e))
        raise ValueError(error_msg) from e


async def authenticate_with_cookies(cookie_string: str) -> None:
    """Authenticate with browser session cookies, bypassing /auth/login/ entirely.

    Monarch CAPTCHA-gates programmatic password login; cookie auth is the supported
    way around that. Requires session_id and csrftoken from a logged-in browser.
    """
    global auth_state, auth_error, auth_failed_at

    if mm_client is None:
        raise RuntimeError("authenticate_with_cookies called before the client was created")

    try:
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        with contextlib.redirect_stdout(stdout_capture), contextlib.redirect_stderr(stderr_capture):
            await mm_client.login_with_cookies(cookie_string, save_session=False)
        persist_session(mm_client)

        auth_state = AuthState.AUTHENTICATED
        auth_error = None
        log.info("auth_success", method="cookies")

    except Exception as e:
        error_msg = (
            f"Cookie authentication failed ({type(e).__name__}). Cookies expire -- copy fresh "
            "session_id and csrftoken values from a logged-in app.monarch.com session."
        )
        log.error("auth_cookie_failed", **safe_error_fields(e))
        mark_auth_failed(error_msg, error_category(e))
        raise ValueError(error_msg) from e


MISSING_CREDENTIALS_MESSAGE = (
    "MONARCH_EMAIL and MONARCH_PASSWORD environment variables are required "
    "(or set MONARCH_TOKEN, or MONARCH_COOKIES, to reuse a browser session)"
)


async def initialize_client() -> None:
    """Initialize the MonarchMoney client with authentication.

    Order:
    1. A saved session file, if present -- needs no credentials at all. It is not
       validated here; validation happens on the first API call.
    2. Fallback credentials (stdio only): MONARCH_TOKEN, then MONARCH_COOKIES, then
       MONARCH_EMAIL/MONARCH_PASSWORD(/MONARCH_MFA_SECRET).

    With no usable session and no fallback, fails with guidance instead of looping.
    """
    global mm_client, auth_state, auth_error, auth_failed_at

    fallbacks = fallback_credentials()
    session_only = not fallbacks

    # Optional MFA/TOTP tracing, off unless MONARCH_DEBUG_MFA=true. debug_mfa.py is a
    # local-only tool (gitignored), so treat it as absent rather than required.
    if os.getenv("MONARCH_DEBUG_MFA", "").lower() == "true":
        try:
            import debug_mfa

            debug_mfa.enable()
        except ImportError:
            log.warning("auth_debug_mfa_unavailable", reason="debug_mfa.py not present")

    # clear_session() resets mm_client to None, so every call to it must happen before
    # the client is constructed -- or the client must be rebuilt afterwards.
    force_login = RUNTIME.transport == "stdio" and os.getenv("MONARCH_FORCE_LOGIN") == "true" and not session_only
    if force_login:
        log.info("auth_force_login")
        clear_session(reason="forced login requested")

    # Try the saved session first -- it needs no credentials (unless forced to skip).
    if session_file.exists() and not force_login:
        try:
            client = load_client_from_session_file()
        except SessionFileError as e:
            log.warning("auth_session_load_failed", **safe_error_fields(e))
            if session_only:
                raise SessionUnavailableError(
                    f"The saved Monarch session could not be loaded: {e}. {auth_recovery_hint()}"
                ) from e
        else:
            mm_client = client
            auth_state = AuthState.AUTHENTICATED
            auth_error = None
            log.info("auth_session_loaded", credential_mode="session_only" if session_only else "with_fallback")
            return

    if session_only:
        if RUNTIME.transport != "stdio":
            raise SessionUnavailableError("No Monarch session is provisioned on this server. " + REPROVISION_HINT)
        log.error("auth_missing_credentials")
        raise SessionUnavailableError(f"{MISSING_CREDENTIALS_MESSAGE}. Or: {BROWSER_AUTH_HINT}")

    email = os.getenv("MONARCH_EMAIL")
    password = os.getenv("MONARCH_PASSWORD")
    mfa_secret = os.getenv("MONARCH_MFA_SECRET")
    cookies = os.getenv("MONARCH_COOKIES")
    token = os.getenv("MONARCH_TOKEN")

    log.info("auth_init", method=fallbacks[0])

    mm_client = MonarchMoney()

    # Token first: it is the only browser-derived credential Monarch's GraphQL API accepts.
    if token:
        await authenticate_with_token(token)
        return

    if cookies:
        await authenticate_with_cookies(cookies)
        return

    # Perform fresh authentication
    max_retries = 2
    retry_delay = 3

    for attempt in range(max_retries):
        try:
            log.info("auth_login_attempt", attempt=attempt + 1, max_retries=max_retries, mfa=bool(mfa_secret))
            if mfa_secret:
                await mm_client.login(email, password, mfa_secret_key=mfa_secret, use_saved_session=False)
            else:
                await mm_client.login(email, password, use_saved_session=False)

            persist_session(mm_client)

            auth_state = AuthState.AUTHENTICATED
            auth_error = None
            log.info("auth_success")
            return

        except RequireMFAException as e:
            error_msg = "Multi-factor authentication required but MONARCH_MFA_SECRET not set"
            log.error("auth_mfa_required")
            mark_auth_failed(error_msg, "auth")
            raise ValueError(error_msg) from e

        except Exception as e:
            # Retrying a CAPTCHA gate cannot succeed and escalates the block.
            if is_captcha_error(e):
                log.error("auth_captcha_required", **safe_error_fields(e))
                mark_auth_failed(CAPTCHA_GUIDANCE, "captcha")
                raise ValueError(CAPTCHA_GUIDANCE) from e

            if attempt < max_retries - 1:
                log.warning("auth_attempt_failed", attempt=attempt + 1, **safe_error_fields(e))
                if is_auth_error(e):
                    clear_session(reason=f"auth failure on attempt {attempt + 1}")
                    mm_client = MonarchMoney()
                await asyncio.sleep(seconds_until_retry(retry_delay, bool(mfa_secret)))
            else:
                log.error("auth_failed", max_retries=max_retries, **safe_error_fields(e))
                mark_auth_failed(str(e), error_category(e))
                raise


async def ensure_authenticated() -> None:
    """Ensure the client is authenticated, initializing on-demand if needed.

    This function uses a lock to prevent concurrent initialization attempts
    and returns immediately if already authenticated.

    Recovery from FAILED state:
    - With fallback credentials: after AUTH_RETRY_COOLDOWN_SECONDS.
    - Session-only: only once the session file changes (a fresh one was provisioned),
      so a dead session is never retried against Monarch in a loop.

    Call this at the start of every tool that needs the mm_client.
    """
    global mm_client, auth_state, auth_lock, auth_error, auth_failed_at, failed_session_mtime

    # Initialize lock on first call (must be done in async context)
    if auth_lock is None:
        auth_lock = asyncio.Lock()

    # Fast path
    if auth_state == AuthState.AUTHENTICATED and mm_client is not None:
        return

    log.info("auth_needed", state=auth_state.value)

    async with auth_lock:
        if auth_state == AuthState.AUTHENTICATED and mm_client is not None:
            return

        if auth_state == AuthState.FAILED:
            if not fallback_credentials():
                current_mtime = session_file_mtime()
                if current_mtime is not None and current_mtime != failed_session_mtime:
                    log.info("auth_session_file_changed")
                    auth_state = AuthState.NOT_INITIALIZED
                    auth_error = None
                    auth_failed_at = None
                else:
                    raise SessionUnavailableError(
                        f"Monarch authentication is unavailable: {auth_error or 'no usable session'}"
                        + ("" if auth_recovery_hint() in (auth_error or "") else f" {auth_recovery_hint()}")
                    )
            elif auth_failed_at is not None:
                elapsed = time.time() - auth_failed_at
                if elapsed < AUTH_RETRY_COOLDOWN_SECONDS:
                    remaining = AUTH_RETRY_COOLDOWN_SECONDS - elapsed
                    error_msg = (
                        f"Authentication previously failed: {auth_error or 'unknown error'}. "
                        f"Cooldown active: retry available in {remaining:.0f} seconds. "
                        f"{BROWSER_AUTH_HINT} It bypasses the cooldown by seeding a fresh session."
                    )
                    raise ValueError(error_msg)
                log.info("auth_cooldown_elapsed", cooldown_s=AUTH_RETRY_COOLDOWN_SECONDS)
                auth_state = AuthState.NOT_INITIALIZED
                auth_error = None
                auth_failed_at = None
            else:
                error_msg = f"Authentication previously failed: {auth_error or 'unknown error'}"
                raise ValueError(error_msg)

        if auth_state == AuthState.INITIALIZING:
            log.warning("auth_already_initializing")
            await asyncio.sleep(1)
            if auth_state == AuthState.AUTHENTICATED:
                return
            raise ValueError("Authentication is taking too long")

        auth_state = AuthState.INITIALIZING
        try:
            await initialize_client()
            log.info("auth_lazy_init_success")
        except Exception as e:
            if auth_state != AuthState.FAILED:
                mark_auth_failed(str(e), error_category(e))
            else:
                failed_session_mtime = session_file_mtime()
            log.error("auth_init_failed", **safe_error_fields(e))
            raise


# FastMCP Tool definitions using decorators


@mcp.tool(annotations=READONLY, title="Get Accounts")
@track_usage
async def get_accounts() -> AccountsResult:
    """Retrieve all linked financial accounts."""
    await ensure_authenticated()

    try:
        accounts = await api_call_with_retry("get_accounts")
        accounts = convert_dates_to_strings(accounts)
        account_list = extract_list(accounts, "accounts")
        return AccountsResult(accounts=account_list, count=len(account_list))
    except Exception as e:
        log.error("Failed to fetch accounts", **safe_error_fields(e))
        raise


@mcp.tool(annotations=READONLY, title="Get Transactions")
@track_usage
async def get_transactions(
    limit: int = 100,
    offset: int = 0,
    start_date: str | None = None,
    end_date: str | None = None,
    account_id: str | None = None,
    category_id: str | None = None,
    tag_ids: str | None = None,
    has_attachments: bool | None = None,
    has_notes: bool | None = None,
    hidden_from_reports: bool | None = None,
    is_split: bool | None = None,
    is_recurring: bool | None = None,
    verbose: bool = False,
) -> TransactionsResult:
    """Fetch transactions with flexible date filtering and smart output formatting.

    Args:
        limit: Maximum number of transactions to return (default: 100, max: 1000)
        offset: Number of transactions to skip for pagination (default: 0)
        start_date: Filter transactions from this date onwards. Supports natural language like 'last month', 'yesterday', '30 days ago'
                    NOTE: If you provide start_date without end_date, end_date will auto-default to 'today'
        end_date: Filter transactions up to this date. Supports natural language
                  NOTE: If you provide end_date without start_date, start_date will auto-default to 'this month'
        account_id: Filter by specific account ID (converted to list internally)
        category_id: Filter by specific category ID (converted to list internally)
        tag_ids: Comma-separated tag IDs to filter by (e.g., "tag1,tag2")
        has_attachments: Filter to transactions with (True) or without (False) attachments
        has_notes: Filter to transactions with (True) or without (False) notes
        hidden_from_reports: Include hidden transactions (True), exclude them (False), or show all (None)
        is_split: Filter to split transactions only (True) or non-split (False)
        is_recurring: Filter to recurring transactions only (True) or non-recurring (False)
        verbose: Output format control (default: False)
            - False (compact mode): Returns essential fields only (~80% smaller)
                Fields included: id, date, amount, merchant, plaidName, category,
                                account, pending, needsReview, notes

            - True (verbose mode): Returns ALL fields including:
                Essential fields (same as compact) PLUS:
                • hideFromReports (bool)
                • reviewStatus (str: "needs_review" | "reviewed" | null)
                • isSplitTransaction (bool)
                • isRecurring (bool)
                • attachments (list of attachment objects)
                • tags (list of tag objects)
                • createdAt (ISO timestamp)
                • updatedAt (ISO timestamp)
                • __typename (GraphQL metadata)
                • Full nested objects with all their fields

            Use verbose=False for most queries to reduce token usage.
            Use verbose=True when you need: timestamps, split info, attachment details,
            or are updating transactions (need full context).

    Key Transaction Fields:
        Core Identifiers:
            - id: Unique transaction ID (required for updates)
            - date: Transaction date (YYYY-MM-DD format)
            - amount: Transaction amount (negative = expense, positive = income)

        Merchant Information:
            IMPORTANT: Monarch normalizes merchant names for cleaner UI
            - merchant.name: User-facing display name shown in Monarch UI (normalized/cleaned)
                Example: "Chipotle" for all Chipotle locations
            - plaidName: Original bank statement text from Plaid/institution (raw data)
                Example: "CHIPOTLE 4963", "CHIPOTLE MEX GR ONLINE", "CHIPOTLE 1879"
                Use this to see location numbers or original descriptors
            - Multiple transactions from different locations share the same merchant.name
            - Use plaidName to distinguish between specific locations/variants

        Categorization:
            - category.id: Category ID (for filtering/updates)
            - category.name: Category display name (e.g., "Restaurants & Bars")
            - tags: List of tag objects applied to transaction

        Account Info:
            - account.id: Account ID where transaction occurred
            - account.displayName: Account name (e.g., "Main Credit Card")

        Status Flags:
            - pending: True if transaction hasn't cleared yet
            - needsReview: True if flagged for user review
            - reviewStatus: "needs_review", "reviewed", or null
            - hideFromReports: True if hidden from budget/reports

        Transaction Types:
            - isSplitTransaction: True if split into multiple categories
            - isRecurring: True if part of a recurring series

        User Annotations:
            NOTE: These are different fields with different purposes
            - notes: Free-form user memo/annotation (e.g., "Business lunch with client")
            - merchant_name: The merchant's display name (e.g., "Olive Garden")
            - Both are editable, but serve different purposes in the UI
            - attachments: List of receipt/document attachments

        Metadata (verbose mode only):
            - createdAt: When transaction was first imported
            - updatedAt: Last modification timestamp
            - __typename: GraphQL type information

    Returns:
        JSON string containing transaction list

    Common Filter Examples:
        - Unreviewed transactions: has_notes=False, needs_review=True
        - Split transactions: is_split=True
        - Transactions with receipts: has_attachments=True
        - Manual transactions: synced_from_institution=False
    """
    await ensure_authenticated()

    try:
        filters = _build_transaction_filters(
            start_date,
            end_date,
            account_id,
            category_id,
            tag_ids,
            has_attachments,
            has_notes,
            hidden_from_reports,
            is_split,
            is_recurring,
        )

        response = await api_call_with_retry("get_transactions", limit=limit, offset=offset, **filters)
        transactions = extract_transactions_list(response)
        transactions = convert_dates_to_strings(transactions)

        if not verbose and isinstance(transactions, list):
            transactions = format_transactions_compact(transactions)

        log.info("Transactions retrieved", count=len(transactions))
        # model_validate (not the constructor) sidesteps mypy's list invariance:
        # list[dict[str, Any]] is not assignable to the model's list[JsonValue].
        return TransactionsResult.model_validate(
            {"transactions": transactions, "count": len(transactions), "verbose": verbose}
        )
    except Exception as e:
        log.error("Failed to fetch transactions", **safe_error_fields(e))
        raise


@mcp.tool(annotations=READONLY, title="Search Transactions")
@track_usage
async def search_transactions(
    query: str,
    limit: int = 500,
    offset: int = 0,
    start_date: str | None = None,
    end_date: str | None = None,
    account_id: str | None = None,
    category_id: str | None = None,
    tag_ids: str | None = None,
    has_attachments: bool | None = None,
    has_notes: bool | None = None,
    hidden_from_reports: bool | None = None,
    is_split: bool | None = None,
    is_recurring: bool | None = None,
    verbose: bool = False,
) -> SearchResult:
    """Search transactions by text using Monarch Money's built-in search.

    Searches merchant names, descriptions, notes, and other fields.
    Accepts all the same filters as get_transactions plus a search query.
    Returns compact results by default (use verbose=True for full details).

    Args:
        query: Search term to find in transactions
        limit: Maximum transactions to return (default: 500, max: 1000)
        offset: Number of transactions to skip for pagination
        start_date: Filter from this date (supports natural language like 'last month')
        end_date: Filter to this date (supports natural language)
        account_id: Filter by specific account ID
        category_id: Filter by specific category ID
        tag_ids: Comma-separated tag IDs to filter by
        has_attachments: Filter by attachment presence
        has_notes: Filter by notes presence
        hidden_from_reports: Filter by report visibility
        is_split: Filter split transactions
        is_recurring: Filter recurring transactions
        verbose: False=compact fields, True=all fields

    Returns:
        JSON with search_metadata and matching transactions
    """
    await ensure_authenticated()

    if not query or not query.strip():
        raise ValueError("Query parameter cannot be empty")

    try:
        query_str = query.strip()
        filters = _build_transaction_filters(
            start_date,
            end_date,
            account_id,
            category_id,
            tag_ids,
            has_attachments,
            has_notes,
            hidden_from_reports,
            is_split,
            is_recurring,
        )
        filters["search"] = query_str

        response = await api_call_with_retry("get_transactions", limit=limit, offset=offset, **filters)
        transactions = extract_transactions_list(response)
        transactions = convert_dates_to_strings(transactions)

        if not verbose:
            transactions = format_transactions_compact(transactions)

        metadata = SearchMetadata(
            query=query_str,
            result_count=len(transactions),
            filters_applied={k: v for k, v in filters.items() if k != "search"},
        )

        log.info("Search complete", result_count=len(transactions))
        # model_validate (not the constructor) sidesteps mypy's list invariance.
        return SearchResult.model_validate({"search_metadata": metadata, "transactions": transactions})

    except Exception as e:
        log.error("Failed to search transactions", **safe_error_fields(e))
        raise


@mcp.tool(annotations=READONLY, title="Get Budgets")
@track_usage
async def get_budgets(start_date: str | None = None, end_date: str | None = None) -> BudgetsResult:
    """Retrieve budget information with flexible date filtering.

    Args:
        start_date: Filter budgets from this date onwards. Supports natural language like 'last month', 'this year'
        end_date: Filter budgets up to this date. Supports natural language

    Returns:
        JSON string containing budget information
    """
    await ensure_authenticated()

    # Use build_date_filter for consistent natural language date support
    kwargs = build_date_filter(start_date, end_date)

    try:
        budgets = await api_call_with_retry("get_budgets", **kwargs)  # type: ignore[arg-type]
        budgets = convert_dates_to_strings(budgets)
        return BudgetsResult(budgets=budgets)
    except Exception as e:
        # Handle the case where no budgets exist
        if "Something went wrong while processing: None" in str(e):
            return BudgetsResult(budgets=[], message="No budgets configured in your Monarch Money account")
        else:
            # Re-raise other errors
            raise


@mcp.tool(annotations=READONLY, title="Get Cashflow")
@track_usage
async def get_cashflow(start_date: str | None = None, end_date: str | None = None) -> CashflowResult:
    """Analyze cashflow data with flexible date filtering.

    Args:
        start_date: Filter cashflow from this date onwards. Supports natural language like 'last month', 'this year'
        end_date: Filter cashflow up to this date. Supports natural language

    Returns:
        JSON string containing cashflow analysis
    """
    await ensure_authenticated()

    # Use build_date_filter for consistent natural language date support
    kwargs = build_date_filter(start_date, end_date)

    cashflow = await api_call_with_retry("get_cashflow", **kwargs)  # type: ignore[arg-type]
    cashflow = convert_dates_to_strings(cashflow)
    return CashflowResult(cashflow=cashflow)


@mcp.tool(annotations=READONLY, title="Get Transaction Categories")
@track_usage
async def get_transaction_categories(verbose: bool = False) -> CategoriesResult:
    """List all transaction categories.

    Args:
        verbose: Output format control (default: False)
            - False: Returns compact format with just {id, name} per category (~80% smaller).
                     Ideal for category lookups when mapping names to IDs.
            - True: Returns full category details including group, order, timestamps, system flags.

    Returns:
        JSON string containing category list
    """
    await ensure_authenticated()

    categories = await api_call_with_retry("get_transaction_categories")
    categories = convert_dates_to_strings(categories)
    category_list = extract_list(categories, "categories")

    if not verbose:
        category_list = [
            {"id": cat.get("id"), "name": cat.get("name")} for cat in category_list if isinstance(cat, dict)
        ]

    return CategoriesResult(categories=category_list, count=len(category_list), verbose=verbose)


@mcp.tool(annotations=WRITE_CREATE, title="Create Transaction")
@track_usage
async def create_transaction(
    amount: float,
    merchant_name: str,
    account_id: str,
    date: str,
    category_id: str,
    notes: str | None = None,
    update_balance: bool = False,
) -> TransactionResult:
    """Create a new manual transaction.

    Args:
        amount: Transaction amount (positive for income, negative for expense)
        merchant_name: Name of the merchant/payee (e.g., "Starbucks", "Monthly Rent")
        account_id: ID of the account for this transaction
        date: Transaction date in YYYY-MM-DD format
        category_id: ID of the category to assign (required for new transactions)
        notes: Optional notes/memo for this transaction
        update_balance: Whether to update account balance when creating this transaction (default: False)
            - False: Transaction is recorded but doesn't affect account balance (typical for synced accounts)
            - True: Adjusts account balance by transaction amount (useful for manual accounts)

    Returns:
        JSON string with created transaction details

    If this fails with "may already have been created", the request reached Monarch
    before the connection failed. Query for the transaction before retrying so it is
    not created twice.
    """
    require_tool_enabled("create_transaction")

    if not merchant_name or merchant_name.strip() == "":
        raise ValueError("merchant_name cannot be empty")
    if not account_id:
        raise ValueError("account_id is required when creating transactions")
    if not category_id:
        raise ValueError("category_id is required when creating transactions")
    date_str = parse_iso_date(date, "date")

    await ensure_authenticated()

    result = await write_call(
        "create_transaction",
        creates=True,
        description="The transaction",
        amount=amount,
        merchant_name=merchant_name,
        category_id=category_id,
        account_id=account_id,
        date=date_str,
        notes=notes or "",
        update_balance=update_balance,
    )
    return TransactionResult(transaction=convert_dates_to_strings(result))


@mcp.tool(annotations=WRITE_REPLACE, title="Update Transaction")
@track_usage
async def update_transaction(
    transaction_id: str,
    amount: float | None = None,
    merchant_name: str | None = None,
    category_id: str | None = None,
    date: str | None = None,
    notes: str | None = None,
    goal_id: str | None = None,
    hide_from_reports: bool | None = None,
    needs_review: bool | None = None,
) -> TransactionResult:
    """Update an existing transaction.

    Args:
        transaction_id: ID of the transaction to update (required)
        amount: New transaction amount
        merchant_name: New merchant display name shown in Monarch UI
            - This updates the user-facing name (merchant.name field)
            - Does NOT change plaidName (original bank statement text, read-only)
            - Empty strings are ignored by the API
            - Example: Change "AMZN Mktp US" to "Amazon"
        category_id: ID of the new category to assign
        date: New transaction date in YYYY-MM-DD format
        notes: User notes/memo for this transaction (separate from merchant name)
            NOTE: This is different from merchant_name
            - notes: Free-form user memo/annotation (e.g., "Business lunch with client")
            - merchant_name: The merchant's display name (e.g., "Olive Garden")
            - Both are editable, but serve different purposes in the UI
            - Use empty string "" to clear existing notes
        goal_id: ID of savings goal to associate with this transaction
            - Use empty string "" to clear goal association
        hide_from_reports: Whether to hide this transaction from reports/analytics
        needs_review: Flag transaction as needing review

    Field Editability:
        Editable Fields (can be updated):
            - amount: Transaction amount
            - merchant_name: User-facing merchant display name
            - category_id: Category assignment
            - date: Transaction date
            - notes: User memo/notes
            - goal_id: Goal association
            - hide_from_reports: Visibility in reports
            - needs_review: Review flag

        Read-Only Fields (cannot be updated):
            - id: Transaction ID (immutable)
            - plaidName: Original bank statement text (from institution)
            - account: Account where transaction occurred
            - pending: Pending status (controlled by institution)
            - createdAt: Creation timestamp
            - isSplitTransaction: Split status (use separate split API)
            - attachments: Use separate attachment API

    Returns:
        JSON string with updated transaction details

    Common Use Cases:
        - Change merchant: merchant_name="Starbucks"
        - Add note: notes="Business expense"
        - Recategorize: category_id="cat_groceries_123"
        - Mark for review: needs_review=True
        - Clear notes: notes=""

    If this fails with "may or may not have been applied", re-read the transaction
    before sending the update again.
    """
    require_tool_enabled("update_transaction")

    updates = build_transaction_update(
        TransactionUpdate(
            transaction_id=transaction_id,
            amount=amount,
            merchant_name=merchant_name,
            category_id=category_id,
            date=date,
            notes=notes,
            goal_id=goal_id,
            hide_from_reports=hide_from_reports,
            needs_review=needs_review,
        )
    )

    await ensure_authenticated()

    log.info("updating_transaction", fields=sorted(k for k in updates if k != "transaction_id"))
    result = await write_call("update_transaction", creates=False, description="The transaction update", **updates)
    return TransactionResult(transaction=convert_dates_to_strings(result))


@mcp.tool(annotations=WRITE_REPLACE, title="Bulk Update Transactions")
@track_usage
async def update_transactions_bulk(updates: str) -> BulkUpdateResult:
    """Update multiple transactions in a single call to save round-trips.

    This is much more efficient than calling update_transaction multiple times.
    Updates are executed in parallel for maximum performance.

    Args:
        updates: JSON string containing list of transaction updates. Each update should have:
            - transaction_id (required): ID of transaction to update
            - amount (optional): New amount
            - merchant_name (optional): New merchant display name
            - category_id (optional): New category ID
            - date (optional): New date in YYYY-MM-DD format
            - notes (optional): New notes
            - goal_id (optional): Goal ID or empty string to clear
            - hide_from_reports (optional): Boolean visibility flag
            - needs_review (optional): Boolean review flag

    Example:
        [
            {"transaction_id": "123", "category_id": "cat_456", "notes": "Updated"},
            {"transaction_id": "789", "merchant_name": "Starbucks", "needs_review": false}
        ]

    Returns:
        JSON with results for each transaction including successes and any failures

    Safety:
        The whole batch is validated before any update is sent: it is rejected, with
        nothing changed, if it exceeds MONARCH_MAX_BULK_UPDATES (default 25), repeats a
        transaction_id, or has any item with a missing id, an unknown field, or a
        wrongly-typed value. If authentication fails mid-batch, updates not yet started
        are reported as "skipped".
    """
    require_tool_enabled("update_transactions_bulk")

    parsed = parse_bulk_updates(updates)
    if not parsed:
        return BulkUpdateResult(
            summary=BulkSummary(total=0, succeeded=0, failed=0), results=[], message="No updates provided"
        )

    await ensure_authenticated()
    log.info("bulk_update_start", count=len(parsed))

    semaphore = asyncio.Semaphore(BULK_UPDATE_CONCURRENCY)
    auth_failed = asyncio.Event()

    async def update_single(update: TransactionUpdate) -> BulkItemResult:
        async with semaphore:
            if auth_failed.is_set():
                return BulkItemResult(
                    transaction_id=update.transaction_id,
                    status="skipped",
                    error="Not attempted: authentication failed earlier in this batch",
                )
            try:
                await write_call(
                    "update_transaction",
                    creates=False,
                    description=f"The update to transaction {update.transaction_id}",
                    max_auth_retries=0,
                    **build_transaction_update(update),
                )
            except Exception as e:
                if isinstance(e, SessionUnavailableError) or is_auth_error(e):
                    auth_failed.set()
                return BulkItemResult(transaction_id=update.transaction_id, status="error", error=scrub_text(str(e)))
            return BulkItemResult(transaction_id=update.transaction_id, status="success")

    results = await asyncio.gather(*[update_single(update) for update in parsed])

    succeeded = sum(1 for r in results if r.status == "success")
    skipped = sum(1 for r in results if r.status == "skipped")
    failed = len(results) - succeeded - skipped

    log.info("bulk_update_complete", succeeded=succeeded, failed=failed, skipped=skipped)

    return BulkUpdateResult(
        summary=BulkSummary(total=len(results), succeeded=succeeded, failed=failed, skipped=skipped),
        results=list(results),
    )


@mcp.tool(annotations=READONLY, title="Get Transaction Splits")
@track_usage
async def get_transaction_splits(transaction_id: str) -> TransactionSplitsResult:
    """Get the split legs of a transaction.

    Splitting lets a single transaction be divided across multiple categories
    (e.g. a Target run that is part groceries, part household). This returns the
    current split legs, if any.

    Args:
        transaction_id: ID of the transaction to inspect

    Returns:
        The transaction id, whether it currently has splits, and the list of
        split legs (each with its own amount, category, merchant, and notes).
        ``splits`` is empty for an un-split transaction.
    """
    await ensure_authenticated()

    try:
        result = await api_call_with_retry("get_transaction_splits", transaction_id=transaction_id)
        result = convert_dates_to_strings(result)
        transaction = result.get("getTransaction") or {} if isinstance(result, dict) else {}
        splits = transaction.get("splitTransactions") or []
        return TransactionSplitsResult(
            transaction_id=transaction_id,
            has_split_transactions=bool(splits),
            splits=splits,
        )
    except Exception as e:
        log.error("Failed to get transaction splits", **safe_error_fields(e))
        raise


@mcp.tool(annotations=WRITE_REPLACE, title="Update Transaction Splits")
@track_usage
async def update_transaction_splits(transaction_id: str, splits: list[TransactionSplit]) -> UpdateSplitsResult:
    """Create, replace, or remove the splits on a transaction.

    This is a full replacement: the splits you pass become the transaction's
    complete set of split legs, replacing any that exist. Pass an empty list to
    remove all splits and restore the transaction to a single un-split entry.

    Args:
        transaction_id: ID of the transaction to split (required)
        splits: The complete set of split legs. Each leg has:
            - amount (required): Leg amount, using the parent's sign convention
              (expenses negative, income positive). All leg amounts MUST sum to
              the parent transaction's amount or Monarch rejects the update.
            - category_id (optional): Category for this leg
            - merchant_name (optional): Merchant display name for this leg;
              defaults to the parent merchant when omitted
            - notes (optional): Per-leg memo
            Pass an empty list to delete all existing splits.

    Example:
        Split a -100.00 transaction into groceries and household:
            transaction_id="txn_123"
            splits=[
                {"amount": -70.00, "category_id": "cat_groceries", "notes": "Food"},
                {"amount": -30.00, "category_id": "cat_household"},
            ]

    Returns:
        The transaction id, whether it now has splits, the resulting split legs,
        and a human-readable summary message.

    If this fails with "may or may not have been applied", read the splits back with
    get_transaction_splits before sending them again.
    """
    require_tool_enabled("update_transaction_splits")
    if not transaction_id:
        raise ValueError("transaction_id is required")

    # Translate our snake_case inputs into the camelCase shape the API expects.
    split_data: list[dict[str, Any]] = []
    for split in splits:
        entry: dict[str, Any] = {"amount": split.amount}
        # Always send merchantName (empty string => inherit the parent merchant),
        # matching the documented split payload shape.
        entry["merchantName"] = split.merchant_name if split.merchant_name is not None else ""
        if split.category_id is not None:
            entry["categoryId"] = split.category_id
        if split.notes is not None:
            entry["notes"] = split.notes
        split_data.append(entry)

    await ensure_authenticated()

    log.info("updating_transaction_splits", split_count=len(split_data))

    result = await write_call(
        "update_transaction_splits",
        creates=False,
        description="The split update",
        transaction_id=transaction_id,
        split_data=split_data,
    )
    result = convert_dates_to_strings(result)

    payload = result.get("updateTransactionSplit") or {} if isinstance(result, dict) else {}
    errors = payload.get("errors")
    if errors:
        raise ValueError(f"Monarch rejected the split update: {errors}")

    transaction = payload.get("transaction") or {}
    result_splits = transaction.get("splitTransactions") or []
    message = (
        f"Removed all splits from transaction {transaction_id}"
        if not split_data
        else f"Set {len(result_splits)} split(s) on transaction {transaction_id}"
    )
    return UpdateSplitsResult(
        transaction_id=transaction_id,
        has_split_transactions=bool(transaction.get("hasSplitTransactions")),
        splits=result_splits,
        message=message,
    )


@mcp.tool(annotations=READONLY, title="Get Account Holdings")
@track_usage
async def get_account_holdings(account_id: str) -> HoldingsResult:
    """Get investment portfolio data (holdings) for a brokerage account.

    Args:
        account_id: ID of the investment/brokerage account to fetch holdings for.
    """
    await ensure_authenticated()

    try:
        holdings = await api_call_with_retry("get_account_holdings", account_id=account_id)
        holdings = convert_dates_to_strings(holdings)
        return HoldingsResult(holdings=holdings)
    except Exception as e:
        log.error("Failed to fetch account holdings", **safe_error_fields(e))
        raise


@mcp.tool(annotations=READONLY, title="Get Account History")
@track_usage
async def get_account_history(
    account_id: str, start_date: str | None = None, end_date: str | None = None
) -> AccountHistoryResult:
    """Get historical account balance data."""
    await ensure_authenticated()

    kwargs: dict[str, Any] = {"account_id": account_id}
    if start_date:
        kwargs["start_date"] = datetime.strptime(start_date, "%Y-%m-%d").date()
    if end_date:
        kwargs["end_date"] = datetime.strptime(end_date, "%Y-%m-%d").date()

    try:
        history = await api_call_with_retry("get_account_history", **kwargs)
        history = convert_dates_to_strings(history)
        return AccountHistoryResult(account_id=account_id, history=history)
    except Exception as e:
        log.error("Failed to fetch account history", **safe_error_fields(e))
        raise


@mcp.tool(annotations=READONLY, title="Get Institutions")
@track_usage
async def get_institutions() -> InstitutionsResult:
    """Get linked financial institutions."""
    await ensure_authenticated()

    try:
        institutions = await api_call_with_retry("get_institutions")
        institutions = convert_dates_to_strings(institutions)
        return InstitutionsResult(institutions=institutions)
    except Exception as e:
        log.error("Failed to fetch institutions", **safe_error_fields(e))
        raise


@mcp.tool(annotations=READONLY, title="Get Recurring Transactions")
@track_usage
async def get_recurring_transactions() -> RecurringResult:
    """Get scheduled recurring transactions."""
    await ensure_authenticated()

    try:
        recurring = await api_call_with_retry("get_recurring_transactions")
        recurring = convert_dates_to_strings(recurring)
        return RecurringResult(recurring=recurring)
    except Exception as e:
        log.error("Failed to fetch recurring transactions", **safe_error_fields(e))
        raise


@mcp.tool(annotations=WRITE_REPLACE, title="Set Budget Amount")
@track_usage
async def set_budget_amount(category_id: str, amount: float) -> SetBudgetResult:
    """Set the budget amount for a category, replacing its current amount."""
    require_tool_enabled("set_budget_amount")
    if not category_id:
        raise ValueError("category_id is required")

    await ensure_authenticated()

    result = await write_call(
        "set_budget_amount", creates=False, description="The budget change", category_id=category_id, amount=amount
    )
    log.info("Budget amount updated")
    return SetBudgetResult(category_id=category_id, amount=amount, result=convert_dates_to_strings(result))


@mcp.tool(annotations=WRITE_CREATE, title="Create Manual Account")
@track_usage
async def create_manual_account(
    account_name: str,
    account_type: str,
    account_sub_type: str,
    balance: float = 0.0,
    is_in_net_worth: bool = True,
) -> CreateAccountResult:
    """Create a manually tracked account.

    Args:
        account_name: Display name for the new account
        account_type: Monarch account type identifier (e.g. "depository")
        account_sub_type: Monarch account subtype identifier (e.g. "savings")
        balance: Starting balance (default 0)
        is_in_net_worth: Whether the account counts toward net worth (default True)

    If this fails with "may already have been created", check get_accounts before
    retrying so the account is not created twice.
    """
    require_tool_enabled("create_manual_account")
    if not account_name or not account_name.strip():
        raise ValueError("account_name cannot be empty")
    if not account_type or not account_sub_type:
        raise ValueError("account_type and account_sub_type are required")

    await ensure_authenticated()

    result = await write_call(
        "create_manual_account",
        creates=True,
        description="The account",
        account_type=account_type,
        account_sub_type=account_sub_type,
        is_in_net_worth=is_in_net_worth,
        account_name=account_name,
        account_balance=balance,
    )
    log.info("Manual account created")
    return CreateAccountResult(account=convert_dates_to_strings(result))


@mcp.tool(annotations=READONLY, title="Get Spending Summary")
@track_usage
async def get_spending_summary(
    start_date: str | None = None, end_date: str | None = None, group_by: str = "category"
) -> SpendingSummaryResult:
    """Get intelligent spending summary with aggregations.

    Args:
        start_date: Start date (supports natural language like 'last month')
        end_date: End date (supports natural language)
        group_by: Group spending by 'category', 'account', or 'month'
    """
    await ensure_authenticated()

    try:
        log.info("Generating spending summary", group_by=group_by)

        # Get transactions for the period
        filters = build_date_filter(start_date, end_date)
        response = await api_call_with_retry("get_transactions", limit=1000, **filters)  # type: ignore[arg-type]
        # Extract transactions list from nested response structure
        transactions = extract_transactions_list(response)

        # Aggregate spending data
        summary: dict[str, Any] = {
            "groups": {},
            "totals": {"income": 0, "expenses": 0, "net": 0},
        }

        for txn in transactions:
            amount = float(txn.get("amount", 0))

            # Track totals
            totals: dict[str, float] = summary["totals"]
            if amount > 0:
                totals["income"] += amount
            else:
                totals["expenses"] += abs(amount)

            # Group by specified field
            if group_by == "category":
                key = (
                    txn.get("category", {}).get("name", "Uncategorized")
                    if isinstance(txn.get("category"), dict)
                    else "Uncategorized"
                )
            elif group_by == "account":
                key = (
                    txn.get("account", {}).get("name", "Unknown") if isinstance(txn.get("account"), dict) else "Unknown"
                )
            elif group_by == "month":
                txn_date = txn.get("date", "")
                key = txn_date[:7] if len(txn_date) >= 7 else "Unknown"  # YYYY-MM format
            else:
                key = "All"

            groups: dict[str, dict[str, float]] = summary["groups"]
            if key not in groups:
                groups[key] = {"income": 0, "expenses": 0, "net": 0, "count": 0}

            group = groups[key]
            if amount > 0:
                group["income"] += amount
            else:
                group["expenses"] += abs(amount)

            group["net"] += amount
            group["count"] += 1

        summary["totals"]["net"] = summary["totals"]["income"] - summary["totals"]["expenses"]

        # Sort groups by total spending (expenses)
        sorted_groups = dict(sorted(summary["groups"].items(), key=lambda x: x[1]["expenses"], reverse=True))

        totals_data: dict[str, float] = summary["totals"]
        result = SpendingSummaryResult(
            period=Period(start=start_date, end=end_date),
            group_by=group_by,
            groups={
                key: GroupSummary(income=g["income"], expenses=g["expenses"], net=g["net"], count=int(g["count"]))
                for key, g in sorted_groups.items()
            },
            totals=Totals(income=totals_data["income"], expenses=totals_data["expenses"], net=totals_data["net"]),
        )

        log.info(
            "Spending summary generated",
            total_transactions=len(transactions),
            groups_count=len(result.groups),
        )

        return result

    except Exception as e:
        log.error("Failed to generate spending summary", **safe_error_fields(e))
        raise


@mcp.tool(annotations=WRITE_REFRESH, title="Refresh Accounts")
@track_usage
async def refresh_accounts() -> RefreshResult:
    """Ask Monarch to re-sync all linked accounts with their financial institutions."""
    require_tool_enabled("refresh_accounts")

    await ensure_authenticated()

    accounts = extract_list(await api_call_with_retry("get_accounts"), "accounts")
    account_ids = [a["id"] for a in accounts if isinstance(a, dict) and isinstance(a.get("id"), str)]
    if not account_ids:
        return RefreshResult(requested=False, account_count=0, result=None)

    result = await write_call(
        "request_accounts_refresh", creates=False, description="The refresh request", account_ids=account_ids
    )
    log.info("Account refresh requested", account_count=len(account_ids))
    return RefreshResult(requested=True, account_count=len(account_ids), result=convert_dates_to_strings(result))


class AuthStatusResult(BaseModel):
    """Safe authentication diagnostics. Deliberately has no field that could carry an
    email, username, secret, cookie, token, session content, file path, or balance."""

    authenticated: bool
    auth_state: str
    session_file_present: bool
    session_file_permissions_ok: bool | None = None
    session_age_seconds: int | None = None
    session_modified_at: str | None = None
    credential_mode: Literal["session_only", "session_with_fallback"]
    writes_enabled: bool
    transport: Transport
    verified: bool | None = None
    last_failure_category: str | None = None
    hint: str | None = None


@mcp.tool(annotations=READONLY, title="Monarch Auth Status")
@track_usage
async def monarch_auth_status(verify: bool = False) -> AuthStatusResult:
    """Report whether this server has a usable Monarch session, without exposing it.

    Returns only safe diagnostics: auth state, whether a session file is present (and
    its age and permissions), whether writes are enabled, the transport, and whether
    fallback credentials exist. Never returns an email, username, password, MFA secret,
    cookie, token, session content, or any financial data.

    Args:
        verify: If True, make one lightweight Monarch request to confirm the session
            actually works (the response is discarded). Default False (no network call).
    """
    present = False
    permissions_ok: bool | None = None
    age_seconds: int | None = None
    modified_at: str | None = None
    try:
        st = session_file.stat()
    except OSError:
        pass
    else:
        present = True
        permissions_ok = stat.S_IMODE(st.st_mode) & 0o077 == 0
        age_seconds = max(0, int(time.time() - st.st_mtime))
        modified_at = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(timespec="seconds")

    verified: bool | None = None
    category = last_failure_category
    if verify:
        try:
            await ensure_authenticated()
            await api_call_with_retry("get_subscription_details", max_retries=0)
            verified = True
        except Exception as e:
            verified = False
            category = error_category(e)

    authenticated = auth_state == AuthState.AUTHENTICATED and mm_client is not None
    needs_attention = verified is False or auth_state == AuthState.FAILED or (not present and not authenticated)
    return AuthStatusResult(
        authenticated=authenticated,
        auth_state=auth_state.value,
        session_file_present=present,
        session_file_permissions_ok=permissions_ok,
        session_age_seconds=age_seconds,
        session_modified_at=modified_at,
        credential_mode="session_with_fallback" if fallback_credentials() else "session_only",
        writes_enabled=RUNTIME.writes_enabled,
        transport=RUNTIME.transport,
        verified=verified,
        last_failure_category=category,
        hint=auth_recovery_hint() if needs_attention else None,
    )


ELICIT_MESSAGE = (
    "Sign in to Monarch Money in your browser to reconnect this server. "
    "Your session is captured locally and never passes through the assistant."
)


@mcp.tool(annotations=SESSION_REPLACE, title="Sign In With Browser")
@track_usage
async def authenticate_browser_session(ctx: Context | None = None) -> BrowserAuthResult:
    """Sign in to Monarch Money using your browser, then save the session.

    Use this when authentication has failed, especially with a CAPTCHA error or a
    misleading "Your code was invalid" message -- Monarch blocks programmatic password
    login, so reusing a browser session is the reliable path.

    Opens a local page that either detects your Monarch session automatically or accepts
    a pasted Cookie header. Credentials go from the browser straight into the session
    file; they are never returned by this tool.

    Local stdio only: it is never listed or callable when the server runs over HTTP.
    """
    global mm_client

    require_tool_enabled("authenticate_browser_session")

    timeout = float(os.getenv("MONARCH_BROWSER_AUTH_TIMEOUT", "300"))
    capture = browser_auth.CookieCaptureServer()
    url = await capture.start()
    elicitation_id = str(uuid.uuid4())
    elicited = False

    try:
        if ctx is not None:
            try:
                outcome = await ctx.elicit_url(message=ELICIT_MESSAGE, url=url, elicitation_id=elicitation_id)
                elicited = True
                action = getattr(outcome, "action", "accept")
                if action != "accept":
                    log.info("browser_auth_declined", action=action)
                    return BrowserAuthResult(
                        authenticated=False,
                        method=action,
                        message=f"Browser sign-in was {action}ed. Nothing was changed.",
                    )
            except Exception as e:
                # Client does not support URL elicitation -- open the browser ourselves.
                log.warning("browser_auth_elicitation_unavailable", **safe_error_fields(e))

        if not elicited:
            opened = webbrowser.open(url)
            log.info("browser_auth_opened_directly", opened=opened)

        cookie_string, method = await capture.wait(timeout)

        if mm_client is None:
            mm_client = MonarchMoney()
        await authenticate_with_cookies(cookie_string)

        if elicited and ctx is not None:
            try:
                await ctx.session.send_elicit_complete(elicitation_id)
            except Exception as e:
                log.warning("browser_auth_complete_notify_failed", **safe_error_fields(e))

        log.info("browser_auth_success", method=method)
        return BrowserAuthResult(
            authenticated=True,
            method=method,
            message=f"Signed in and saved the session ({method}). Monarch tools are ready to use.",
        )

    except (TimeoutError, asyncio.TimeoutError):
        log.warning("browser_auth_timed_out", timeout_s=timeout)
        return BrowserAuthResult(
            authenticated=False,
            method="timeout",
            message=(
                f"Timed out after {timeout:.0f}s waiting for browser sign-in. "
                f"Run the tool again, or open this link within the time limit: {url}"
            ),
        )
    finally:
        await capture.stop()


@mcp.tool(annotations=READONLY, title="Complete Financial Overview")
@track_usage
async def get_complete_financial_overview(period: str = "this month", ctx: Context | None = None) -> FinancialOverview:
    """Get complete financial overview in a single call - accounts, transactions, budgets, cashflow.

    This intelligent batch tool combines multiple API calls to provide comprehensive financial analysis,
    reducing round-trips and providing deeper insights.

    Args:
        period: Time period for analysis ("this month", "last month", "this year", etc.)
    """
    await ensure_authenticated()

    try:
        if ctx is not None:
            await ctx.report_progress(0, 5, "Fetching accounts, budgets, cashflow, transactions, categories…")

        # Parse the period into date filters
        filters = build_date_filter(period, None)

        # Execute all API calls in parallel for maximum efficiency
        accounts_task = api_call_with_retry("get_accounts")
        budgets_task = api_call_with_retry("get_budgets", **filters)  # type: ignore[arg-type]
        cashflow_task = api_call_with_retry("get_cashflow", **filters)  # type: ignore[arg-type]
        transactions_task = api_call_with_retry("get_transactions", limit=500, **filters)  # type: ignore[arg-type]
        categories_task = api_call_with_retry("get_transaction_categories")

        # Wait for all results
        api_results = await asyncio.gather(
            accounts_task, budgets_task, cashflow_task, transactions_task, categories_task, return_exceptions=True
        )
        accounts, budgets, cashflow, transactions, categories = api_results

        # Handle any exceptions gracefully
        results: dict[str, Any] = {}

        if not isinstance(accounts, Exception):
            results["accounts"] = convert_dates_to_strings(accounts)
        else:
            results["accounts"] = {"error": str(accounts)}

        if not isinstance(budgets, Exception):
            results["budgets"] = convert_dates_to_strings(budgets)
        else:
            results["budgets"] = {"error": str(budgets)}

        if not isinstance(cashflow, Exception):
            results["cashflow"] = convert_dates_to_strings(cashflow)
        else:
            results["cashflow"] = {"error": str(cashflow)}

        if not isinstance(transactions, Exception):
            # Extract transactions list from nested response structure
            transactions_list = extract_transactions_list(transactions)
            results["transactions"] = convert_dates_to_strings(transactions_list)
            # Add intelligent transaction analysis
            if isinstance(transactions_list, list):
                results["transaction_summary"] = {
                    "total_count": len(transactions_list),
                    "total_income": sum(
                        float(t.get("amount", 0)) for t in transactions_list if float(t.get("amount", 0)) > 0
                    ),
                    "total_expenses": sum(
                        abs(float(t.get("amount", 0))) for t in transactions_list if float(t.get("amount", 0)) < 0
                    ),
                    "unique_categories": len(
                        {
                            t.get("category", {}).get("name", "Unknown")
                            for t in transactions_list
                            if isinstance(t.get("category"), dict)
                        }
                    ),
                    "unique_accounts": len(
                        {
                            t.get("account", {}).get("name", "Unknown")
                            for t in transactions_list
                            if isinstance(t.get("account"), dict)
                        }
                    ),
                }
        else:
            results["transactions"] = {"error": str(transactions)}

        if not isinstance(categories, Exception):
            results["categories"] = convert_dates_to_strings(categories)
        else:
            results["categories"] = {"error": str(categories)}

        if ctx is not None:
            await ctx.report_progress(5, 5, "Assembled financial overview")

        # Metadata about the batch operation (leading underscore avoided so it
        # round-trips through the Pydantic model rather than being treated as private).
        results["batch_metadata"] = {
            "period": period,
            "filters_applied": convert_dates_to_strings(filters),
            "api_calls_made": 5,
            "timestamp": datetime.now().isoformat(),
        }
        results["period"] = period

        accounts_val = results.get("accounts", [])
        summary_val = results.get("transaction_summary")
        log.info(
            "Complete financial overview generated",
            period=period,
            accounts_count=len(accounts_val) if isinstance(accounts_val, list) else 0,
            transactions_count=summary_val.get("total_count", 0) if isinstance(summary_val, dict) else 0,
        )

        return FinancialOverview.model_validate(results)

    except Exception as e:
        log.error("Failed to generate financial overview", **safe_error_fields(e))
        raise


@mcp.tool(annotations=READONLY, title="Analyze Spending Patterns")
@track_usage
async def analyze_spending_patterns(
    lookback_months: int = 6, include_forecasting: bool = True, ctx: Context | None = None
) -> SpendingPatterns:
    """Intelligent spending pattern analysis with trend forecasting.

    Combines multiple data sources to provide deep spending insights including:
    - Monthly spending trends by category
    - Account usage patterns
    - Budget performance analysis
    - Predictive spending forecasts

    Args:
        lookback_months: Number of months to analyze (default 6)
        include_forecasting: Whether to include spending forecasts
    """
    await ensure_authenticated()

    try:
        if ctx is not None:
            await ctx.report_progress(0, 2, "Fetching transactions, budgets, accounts, categories…")

        # Calculate date ranges for analysis
        end_date = datetime.now().date()
        start_date = end_date - relativedelta(months=lookback_months)

        # Batch API calls for comprehensive data
        transactions_task = api_call_with_retry(
            "get_transactions", limit=2000, start_date=start_date, end_date=end_date
        )
        budgets_task = api_call_with_retry("get_budgets", start_date=start_date, end_date=end_date)
        accounts_task = api_call_with_retry("get_accounts")
        categories_task = api_call_with_retry("get_transaction_categories")

        api_results = await asyncio.gather(
            transactions_task, budgets_task, accounts_task, categories_task, return_exceptions=True
        )
        transactions, budgets, accounts, categories = api_results

        if ctx is not None:
            await ctx.report_progress(1, 2, "Computing trends, category and account analysis…")

        analysis = {
            "analysis_period": {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "months_analyzed": lookback_months,
            },
            "monthly_trends": {},
            "category_analysis": {},
            "account_usage": {},
            "budget_performance": {},
        }

        if not isinstance(transactions, Exception):
            # Extract transactions list from nested response structure
            transactions_list = extract_transactions_list(transactions)

            # Monthly spending trends
            monthly_data: dict[str, dict[str, float]] = {}
            category_totals: dict[str, dict[str, float]] = {}
            account_usage: dict[str, dict[str, float]] = {}

            for txn in transactions_list:
                txn_date = txn.get("date", "")
                amount = float(txn.get("amount", 0))
                category_name = (
                    txn.get("category", {}).get("name", "Uncategorized")
                    if isinstance(txn.get("category"), dict)
                    else "Uncategorized"
                )
                account_name = (
                    txn.get("account", {}).get("name", "Unknown") if isinstance(txn.get("account"), dict) else "Unknown"
                )

                # Monthly trends (YYYY-MM)
                month_key = txn_date[:7] if len(txn_date) >= 7 else "Unknown"
                if month_key not in monthly_data:
                    monthly_data[month_key] = {"income": 0.0, "expenses": 0.0, "net": 0.0, "transaction_count": 0.0}

                if amount > 0:
                    monthly_data[month_key]["income"] += amount
                else:
                    monthly_data[month_key]["expenses"] += abs(amount)
                monthly_data[month_key]["net"] += amount
                monthly_data[month_key]["transaction_count"] += 1

                # Category analysis
                if category_name not in category_totals:
                    category_totals[category_name] = {"total": 0.0, "transactions": 0.0, "avg_amount": 0.0}
                category_totals[category_name]["total"] += abs(amount) if amount < 0 else 0.0  # Only expenses
                category_totals[category_name]["transactions"] += 1

                # Account usage
                if account_name not in account_usage:
                    account_usage[account_name] = {"total_volume": 0.0, "transactions": 0.0}
                account_usage[account_name]["total_volume"] += abs(amount)
                account_usage[account_name]["transactions"] += 1

            # Calculate averages and sort data
            for category in category_totals:
                if category_totals[category]["transactions"] > 0:
                    category_totals[category]["avg_amount"] = (
                        category_totals[category]["total"] / category_totals[category]["transactions"]
                    )

            analysis["monthly_trends"] = dict(sorted(monthly_data.items()))
            analysis["category_analysis"] = dict(
                sorted(category_totals.items(), key=lambda x: x[1]["total"], reverse=True)  # type: ignore[index]
            )
            analysis["account_usage"] = dict(
                sorted(account_usage.items(), key=lambda x: x[1]["total_volume"], reverse=True)  # type: ignore[index]
            )

            # Simple forecasting if requested
            if include_forecasting and monthly_data:
                recent_months = list(monthly_data.values())[-3:]  # Last 3 months
                if recent_months:
                    avg_monthly_expenses = sum(m["expenses"] for m in recent_months) / len(recent_months)
                    avg_monthly_income = sum(m["income"] for m in recent_months) / len(recent_months)

                    next_month = (end_date + relativedelta(months=1)).strftime("%Y-%m")
                    analysis["forecast"] = {
                        "next_month": next_month,
                        "predicted_expenses": round(avg_monthly_expenses, 2),
                        "predicted_income": round(avg_monthly_income, 2),
                        "predicted_net": round(avg_monthly_income - avg_monthly_expenses, 2),
                        "confidence": "medium",  # Based on 3-month average
                        "note": "Forecast based on 3-month spending average",
                    }

        if not isinstance(budgets, Exception):
            analysis["budget_performance"] = convert_dates_to_strings(budgets)

        # Metadata (leading underscore avoided so it round-trips through the model).
        txn_count = len(transactions_list) if not isinstance(transactions, Exception) else 0
        analysis["metadata"] = {
            "api_calls_made": 4,
            "total_transactions_analyzed": txn_count,
            "analysis_timestamp": datetime.now().isoformat(),
        }

        if ctx is not None:
            await ctx.report_progress(2, 2, "Spending pattern analysis complete")

        log.info(
            "Spending pattern analysis completed",
            lookback_months=lookback_months,
            transactions_analyzed=txn_count,
            include_forecasting=include_forecasting,
        )

        return SpendingPatterns.model_validate(analysis)

    except Exception as e:
        log.error("Failed to analyze spending patterns", **safe_error_fields(e))
        raise


def load_env_file(env_path: Path | None = None) -> int:
    """Load KEY=value pairs from a .env file into the environment.

    Called from the server entry points only -- never at import time -- so that
    importing this module (as the test suite does) has no side effects on the
    environment. Real environment variables always win over .env values.

    Returns the number of variables loaded.
    """
    path = env_path if env_path is not None else Path(__file__).parent / ".env"
    if not path.exists():
        return 0

    loaded = 0
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key not in os.environ:
            os.environ[key] = value
            loaded += 1

    log.info("env_file_loaded", path=str(path), variables=loaded)
    return loaded


def configure_runtime(transport: Transport) -> None:
    """Apply transport-dependent safety defaults. Fails fast on invalid settings."""
    RUNTIME.transport = transport
    RUNTIME.writes_enabled = parse_bool_env("MONARCH_ENABLE_WRITES", default=transport == "stdio")
    RUNTIME.max_bulk_updates = parse_max_bulk_updates()

    if transport == "http":
        # Remote mode is session-file only. Drop credential variables from the process
        # environment so they are neither used nor lingering in memory.
        ignored = [name for name in CREDENTIAL_ENV_VARS if os.environ.pop(name, None)]
        if ignored:
            log.warning("http_mode_ignored_credential_env", variables=ignored)
        for name in ("mcp", "uvicorn", "uvicorn.error"):
            logging.getLogger(name).setLevel(logging.WARNING)

    log.info(
        "runtime_configured",
        transport=transport,
        writes_enabled=RUNTIME.writes_enabled,
        max_bulk_updates=RUNTIME.max_bulk_updates,
        credential_mode="session_with_fallback" if fallback_credentials() else "session_only",
    )


async def provision_session(output_dir: Path | None) -> int:
    """Create a session file on this machine for copying to a remote server.

    Uses MONARCH_TOKEN or MONARCH_COOKIES from the environment when set; otherwise
    opens the local browser sign-in page. Verifies the session with one read, then
    writes session.pickle atomically with 0600 permissions. Never prints credentials.
    """
    global session_dir, session_file

    load_env_file()
    if output_dir is not None:
        session_dir = output_dir.expanduser().resolve()
        session_file = session_dir / "session.pickle"
    secure_session_dir(session_dir)

    token = os.getenv("MONARCH_TOKEN")
    cookies = os.getenv("MONARCH_COOKIES")
    try:
        if token:
            client = MonarchMoney(token=token)
        else:
            if not cookies:
                capture = browser_auth.CookieCaptureServer()
                url = await capture.start()
                print(f"Opening {url} -- sign in to Monarch there to continue.", file=sys.stderr)
                webbrowser.open(url)
                try:
                    cookies, _method = await capture.wait(float(os.getenv("MONARCH_BROWSER_AUTH_TIMEOUT", "300")))
                finally:
                    await capture.stop()
            client = MonarchMoney()
            await client.login_with_cookies(cookies, save_session=False, verify=False)

        with contextlib.redirect_stdout(io.StringIO()):
            accounts = extract_list(await client.get_accounts(), "accounts")
        persist_session(client)
    except Exception as e:
        print(f"Provisioning failed ({type(e).__name__}): {scrub_text(str(e))}", file=sys.stderr)
        return 1

    print(
        f"Session verified ({len(accounts)} accounts visible) and saved to {session_file} with mode 0600.",
        file=sys.stderr,
    )
    return 0


async def main(transport: Transport = "stdio") -> None:
    """Main entry point for the server.

    The server starts immediately without authentication. Authentication
    happens lazily on the first tool call via ensure_authenticated().
    """
    load_env_file()
    configure_runtime(transport)
    log.info("server_starting", transport=transport, auth_state=auth_state.value)

    if transport == "http":
        import http_app

        # FastMCP has no public accessor for its low-level server; it is what the
        # session manager drives.
        await http_app.serve(mcp._mcp_server, http_app.HttpSettings.from_env(os.environ))
        return

    try:
        await mcp.run_stdio_async()
    except (BrokenPipeError, ConnectionResetError):
        log.info("client_disconnected")
    except KeyboardInterrupt:
        log.info("interrupted")
    except Exception as e:
        log.error("server_error", **safe_error_fields(e))
        raise


def parse_transport(value: str) -> Transport:
    normalized = value.strip().lower()
    if normalized == "stdio":
        return "stdio"
    if normalized in ("http", "streamable-http"):
        return "http"
    raise ValueError(f"Unknown transport {value!r}: use stdio or http")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="monarch-mcp-jamiew", description="Monarch Money MCP server")
    parser.add_argument(
        "--transport",
        help="stdio (default) or http (Streamable HTTP on MONARCH_HTTP_HOST:MONARCH_HTTP_PORT/mcp). "
        "Overrides MONARCH_TRANSPORT.",
    )
    parser.add_argument(
        "--provision-session",
        action="store_true",
        help="Sign in once and write session.pickle for copying to a remote server, then exit.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Directory for --provision-session (default: MONARCH_SESSION_DIR or ~/.monarch-mcp).",
    )
    return parser


def run(argv: Sequence[str] | None = None) -> None:
    """Synchronous console-script entry point (`monarch-mcp-jamiew`).

    Wraps the async `main()` so the published entry point actually awaits it.
    """
    args = build_arg_parser().parse_args(argv)
    # Anything this process creates (session files, temp files) is private to the user.
    os.umask(0o077)

    if args.provision_session:
        sys.exit(asyncio.run(provision_session(args.output_dir)))

    try:
        transport = parse_transport(args.transport or os.getenv("MONARCH_TRANSPORT") or "stdio")
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)

    if transport == "stdio":

        def signal_handler(signum: int, frame: Any) -> None:
            log.info("signal_received", signum=signum)
            # Let asyncio handle the shutdown

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

    try:
        asyncio.run(main(transport))
    except (BrokenPipeError, ConnectionResetError):
        pass  # Expected during client disconnect
    except KeyboardInterrupt:
        pass
    except Exception as eg:
        # Handle ExceptionGroups from anyio TaskGroups
        if hasattr(eg, "exceptions"):
            remaining = [
                exc
                for exc in eg.exceptions
                if not isinstance(exc, (BrokenPipeError, ConnectionResetError, OSError, EOFError))
                and not any(s in str(exc).lower() for s in ["broken pipe", "connection reset", "[errno 32]", "eof"])
            ]
            if remaining:
                log.error("fatal_error", **safe_error_fields(eg))
                raise
        else:
            log.error("fatal_error", **safe_error_fields(eg))
            raise


if __name__ == "__main__":
    run()
