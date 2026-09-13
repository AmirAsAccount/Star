"""Two-stage stock screener using cached Finviz data and live Schwab quotes."""

from __future__ import annotations

import ast
import base64
import csv
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Optional

import requests


def load_local_env() -> None:
    """Load basic KEY=VALUE entries from a local .env file."""
    env_path = Path(__file__).resolve().parent / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    except OSError as exc:
        print(f"[WARN] Could not read {env_path}: {exc}")
        return

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[:1] == value[-1:] and value[:1] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


load_local_env()

# Stage 1 reads the fundamentals cache built by storage.py.

CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

FUNDAMENTALS_CACHE_PATH = Path("fundamentals.csv")

_TICKER_LINK_RE = re.compile(r'[?&]t=([A-Za-z][A-Za-z\.\-]{0,5})(?=[&"\'])')
_TOTAL_ROWS_RE = re.compile(
    r'id=["\']screener-total["\'][^>]*>\s*#\s*\d+\s*/\s*([\d,]+)\s+Total',
    re.IGNORECASE,
)

# Shared with storage.py.
FINVIZ_PAGE_DELAY_SEC = 1.5
FINVIZ_BATCH_SIZE = 3
FINVIZ_COOLDOWN_SEC = 5.0


def _fetch_finviz_page(session: requests.Session, page_url: str):
    """Fetch one Finviz page with browser-like headers."""
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://finviz.com/screener.ashx",
    }
    resp = session.get(page_url, headers=headers, timeout=15)
    resp.raise_for_status()
    if resp.text.strip() == "Too many requests.":
        raise RuntimeError("Too many requests.")
    return resp


def _extract_total_rows(html_text: str) -> Optional[int]:
    """Read the total result count from a Finviz page."""
    match = _TOTAL_ROWS_RE.search(html_text)
    return int(match.group(1).replace(",", "")) if match else None


def _dump_debug_html(page_url: str, response) -> str:
    """Save a failed response for debugging."""
    debug_path = CACHE_DIR / "finviz_debug_page.html"
    try:
        debug_path.write_text(response.text, encoding="utf-8", errors="replace")
    except Exception as e:
        print(f"  [DEBUG] Couldn't write debug HTML: {e}")
    text = response.text
    print(f"  [DEBUG] {page_url}")
    print(f"  [DEBUG] status={response.status_code}  content-length={len(text)}")
    print(f"  [DEBUG] contains 'quote.ashx'? {'quote.ashx' in text}   "
          f"contains 't='? {'t=' in text}   contains 'screener'? {'screener' in text.lower()}")
    print(f"  [DEBUG] full HTML saved to {debug_path.resolve()} -- open it and search for one of "
          f"your expected tickers' text to see what markup actually wraps it.")
    return debug_path.as_posix()


def _make_alias(header: str) -> str:
    """Convert a Finviz header into a valid field name."""
    words = re.sub(r"[^0-9A-Za-z]+", " ", header).strip().split()
    alias = "".join(w[:1].upper() + w[1:] for w in words)
    if not alias:
        alias = "FIELD"
    if alias[0].isdigit():
        alias = "_" + alias
    return alias


def _coerce_numeric(raw: str):
    """Convert a Finviz value to a number when possible."""
    if raw is None:
        return None
    s = raw.strip()
    if s in ("", "-"):
        return None
    s = s.replace(",", "")
    suffix_mult = {"K": 1e3, "M": 1e6, "B": 1e9}
    mult = 1.0
    if s and s[-1].upper() in suffix_mult:
        mult = suffix_mult[s[-1].upper()]
        s = s[:-1]
    is_pct = s.endswith("%")
    if is_pct:
        s = s[:-1]
    try:
        value = float(s) * mult
    except ValueError:
        return raw.strip()
    return value


def load_fundamentals_cache(cache_path: Path = FUNDAMENTALS_CACHE_PATH):
    """Load and normalize the fundamentals CSV."""
    if not cache_path.exists():
        raise RuntimeError(
            f"Fundamentals cache not found at {cache_path.resolve()}. "
            f"Run `python storage.py` first to build it.")

    with cache_path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        if not fieldnames:
            raise RuntimeError(f"{cache_path} has no header row -- rebuild it with `python storage.py`.")

        alias_to_header: dict = {}
        header_to_alias: dict = {}
        for header in fieldnames:
            alias = _make_alias(header)
            base_alias = alias
            n = 2
            while alias in alias_to_header and alias_to_header[alias] != header:
                alias = f"{base_alias}_{n}"
                n += 1
            alias_to_header[alias] = header
            header_to_alias[header] = alias

        ticker_alias = header_to_alias.get("Ticker")
        if ticker_alias is None:
            raise RuntimeError(f"{cache_path} has no 'Ticker' column -- rebuild it with `python storage.py`.")

        rows: dict = {}
        for raw_row in reader:
            ticker = (raw_row.get("Ticker") or "").strip().upper()
            if not ticker:
                continue
            coerced = {header_to_alias[h]: _coerce_numeric(v) for h, v in raw_row.items() if h in header_to_alias}
            rows[ticker] = coerced

    if not rows:
        raise RuntimeError(f"{cache_path} contained a header but no data rows -- rebuild it with `python storage.py`.")

    return alias_to_header, header_to_alias, rows


def finviz_screen_from_cache(condition: str, cache_path: Path = FUNDAMENTALS_CACHE_PATH):
    """Return cached tickers that satisfy the fundamentals condition."""
    alias_to_header, header_to_alias, rows = load_fundamentals_cache(cache_path)
    allowed_fields = set(alias_to_header.keys())
    condition = validate_condition(condition, allowed_fields)

    tickers = []
    for ticker, fields in rows.items():
        if _eval_condition(condition, fields, allowed_fields):
            tickers.append(ticker)

    print(f"[FINVIZ-CACHE] {cache_path} -> {len(rows)} cached tickers, "
          f"{len(tickers)} passed \"{condition}\"")
    return sorted(tickers)


# Stage 2: live Schwab quotes.

QUOTE_BATCH_CHUNK_SIZE = 100


class SchwabQuoteClient:
    """Small Schwab client for batched quotes."""

    TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
    BASE_URL = "https://api.schwabapi.com/marketdata/v1"

    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self._access_token: Optional[str] = None
        self._expires_at = 0.0

    def _request_with_retry(self, method: str, url: str, *, max_attempts: int = 3,
                             base_delay: float = 0.25, max_delay: float = 1.5, **kwargs):
        """Retry temporary connection, rate-limit, and server errors."""
        last_exc = None
        for attempt in range(1, max_attempts + 1):
            try:
                resp = requests.request(method, url, **kwargs)
                if resp.status_code == 429 or 500 <= resp.status_code < 600:
                    if attempt == max_attempts:
                        return resp
                else:
                    return resp
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                    requests.exceptions.ChunkedEncodingError) as e:
                last_exc = e
                if attempt == max_attempts:
                    raise
            delay = min(max_delay, base_delay * (2 ** (attempt - 1))) + random.uniform(0, 0.15)
            time.sleep(delay)
        if last_exc:
            raise last_exc

    def _refresh(self):
        creds = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        headers = {"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"}
        payload = {"grant_type": "refresh_token", "refresh_token": self.refresh_token}
        resp = self._request_with_retry("POST", self.TOKEN_URL, headers=headers, data=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data["access_token"]
        self._expires_at = time.time() + data.get("expires_in", 1800) - 60

    def _auth_headers(self) -> dict:
        if self._access_token is None or time.time() >= self._expires_at:
            self._refresh()
        return {"Authorization": f"Bearer {self._access_token}"}

    def get_quotes_batch(self, symbols: list[str]) -> dict:
        """Fetch one batch of quotes."""
        if not symbols:
            return {}
        resp = self._request_with_retry(
            "GET", f"{self.BASE_URL}/quotes", headers=self._auth_headers(),
            params={"symbols": ",".join(symbols)}, timeout=20)
        resp.raise_for_status()
        raw = resp.json()
        out = {}
        for sym in symbols:
            quote = raw.get(sym, {}).get("quote", {})
            if not quote:
                continue
            total_vol = quote.get("totalVolume")
            out[sym] = {
                "bidPrice": quote.get("bidPrice"),
                "bidSize": quote.get("bidSize"),
                "askPrice": quote.get("askPrice"),
                "askSize": quote.get("askSize"),
                "lastPrice": quote.get("lastPrice"),
                "mark": quote.get("mark"),
                "totalVolume": total_vol,
                "volume": total_vol,
            }
        return out

    def get_quotes_for_universe(self, symbols: list[str]) -> dict:
        """Fetch quotes for the full symbol list in batches."""
        merged: dict = {}
        for i in range(0, len(symbols), QUOTE_BATCH_CHUNK_SIZE):
            chunk = symbols[i:i + QUOTE_BATCH_CHUNK_SIZE]
            try:
                merged.update(self.get_quotes_batch(chunk))
            except Exception as e:
                print(f"  [WARN] Quote batch failed for chunk starting {chunk[0]}: {e}")
        return merged


# Condition parsing shared by both stages.

_ALLOWED_FIELDS = ("bidPrice", "bidSize", "askPrice", "askSize",
                   "lastPrice", "mark", "totalVolume", "volume")

_ALLOWED_AST_NODES = (
    ast.Expression, ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare,
    ast.Name, ast.Load, ast.Constant, ast.And, ast.Or, ast.Not,
    ast.USub, ast.UAdd, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod,
    ast.Pow, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.In, ast.NotIn, ast.List, ast.Tuple,
)

_MAGNITUDE_SUFFIX = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000, "T": 1_000_000_000_000}

_NUMERIC_LITERAL_RE = re.compile(
    r'(?<![A-Za-z0-9_.])(\d+(?:\.\d+)?)\s*([KkMmBbTt]\b|%)'
)


def _rewrite_numeric_literals(text: str) -> str:
    """Expand shorthand such as 10M, 1.5B, and 10%."""

    def _replace(m: re.Match) -> str:
        number, suffix = m.group(1), m.group(2)
        if suffix == "%":
            return number
        mult = _MAGNITUDE_SUFFIX[suffix.upper()]
        value = float(number) * mult
        return str(int(value)) if value == int(value) else str(value)

    return _NUMERIC_LITERAL_RE.sub(_replace, text)


def _split_comma_conditions(raw: str) -> list[str]:
    """Split on commas outside strings and brackets."""
    pieces: list[str] = []
    depth = 0
    quote_char = None
    current = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if quote_char:
            current.append(ch)
            if ch == "\\" and i + 1 < len(raw):
                current.append(raw[i + 1])
                i += 2
                continue
            if ch == quote_char:
                quote_char = None
        elif ch in ("'", '"'):
            quote_char = ch
            current.append(ch)
        elif ch in "([{":
            depth += 1
            current.append(ch)
        elif ch in ")]}":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            pieces.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
        i += 1
    tail = "".join(current).strip()
    if tail:
        pieces.append(tail)
    return [p for p in pieces if p]


def validate_condition(condition: str, allowed_fields) -> str:
    """Validate a condition before passing it to eval."""
    try:
        tree = ast.parse(condition, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"invalid Python expression: {exc.msg}") from exc

    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_AST_NODES):
            raise ValueError(f"unsupported syntax: {type(node).__name__}")
        if isinstance(node, ast.Name):
            names.add(node.id)
    unknown = names.difference(allowed_fields)
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
    if not names or not any(isinstance(node, (ast.Compare, ast.BoolOp)) for node in ast.walk(tree)):
        raise ValueError("condition must compare a field, e.g. 'PE > 0' or 'lastPrice > 5'")
    return condition


def validate_quote_condition(condition: str) -> str:
    """Back-compat wrapper: validates against the fixed Schwab quote field set."""
    return validate_condition(condition, _ALLOWED_FIELDS)


def combine_conditions(raw_input: str, allowed_fields) -> str:
    """Validate comma-separated conditions and join them with AND."""
    raw_input = _rewrite_numeric_literals(raw_input)
    pieces = _split_comma_conditions(raw_input)
    if not pieces:
        raise ValueError("no condition entered")
    for piece in pieces:
        validate_condition(piece, allowed_fields)
    return " and ".join(f"({p})" for p in pieces)


def _eval_condition(condition: str, fields: dict, allowed_fields) -> bool:
    """Evaluate a condition, failing closed on missing data."""
    referenced = {n for n in allowed_fields if re.search(rf"\b{re.escape(n)}\b", condition)}
    local_ns = {k: fields.get(k) for k in referenced}
    if any(v is None for v in local_ns.values()):
        return False
    try:
        return bool(eval(condition, {"__builtins__": {}}, local_ns))
    except Exception as e:
        print(f"  [WARN] Condition eval failed ({e}) for row {fields} -- treated as fail.")
        return False


def quote_passes(quote: dict, condition: str) -> bool:
    """Check a quote-only condition."""
    return _eval_condition(condition, quote, _ALLOWED_FIELDS)


def _prompt_for_condition(label: str, allowed_fields) -> str:
    """Prompt until a valid condition is entered."""
    print(f"\n{label} FIELDS: {', '.join(sorted(allowed_fields))}")
    while True:
        raw = input("Enter condition(s), comma-separated (e.g. SharesFloat < 10M, Gap > 10%): ").strip()
        try:
            return combine_conditions(raw, allowed_fields)
        except ValueError as exc:
            print(f"  [INVALID] {exc} -- try again.")


def main():
    # Stage 1: cached fundamentals
    try:
        alias_to_header, header_to_alias, fundamentals_rows = load_fundamentals_cache()
    except RuntimeError as exc:
        print(f"[ERROR] {exc}")
        sys.exit(1)

    finviz_condition = _prompt_for_condition("FINVIZ", alias_to_header.keys())
    tickers = finviz_screen_from_cache(finviz_condition)
    if not tickers:
        print("[DONE] Finviz cache screen returned no tickers -- nothing to quote-check.")
        return []

    # Stage 2: live quotes
    client_id = os.environ.get("SCHWAB_MARKETDATA_CLIENT_ID")
    client_secret = os.environ.get("SCHWAB_MARKETDATA_CLIENT_SECRET")
    refresh_token = os.environ.get("SCHWAB_MARKETDATA_REFRESH_TOKEN")
    if not all([client_id, client_secret, refresh_token]):
        print("[ERROR] Missing one of SCHWAB_MARKETDATA_CLIENT_ID / "
              "SCHWAB_MARKETDATA_CLIENT_SECRET / SCHWAB_MARKETDATA_REFRESH_TOKEN "
              "in the environment.")
        sys.exit(1)

    collisions = set(_ALLOWED_FIELDS) & alias_to_header.keys()
    if collisions:
        print(f"[ERROR] Field name collision between Finviz and Schwab fields: "
              f"{', '.join(sorted(collisions))}. Rename the conflicting Finviz "
              f"column alias (see _make_alias) before combining the two field sets.")
        sys.exit(1)
    combined_fields = set(_ALLOWED_FIELDS) | alias_to_header.keys()

    quote_condition = _prompt_for_condition("SCHWAB + FINVIZ", combined_fields)

    client = SchwabQuoteClient(client_id, client_secret, refresh_token)
    quotes = client.get_quotes_for_universe(tickers)
    print(f"[SCHWAB] {len(quotes)}/{len(tickers)} tickers returned a quote.")

    results: list[list] = []
    for ticker in tickers:
        quote = quotes.get(ticker)
        if not quote:
            continue
        merged_fields = {**fundamentals_rows.get(ticker, {}), **quote}
        if _eval_condition(quote_condition, merged_fields, combined_fields):
            results.append([ticker, quote.get("lastPrice"), quote.get("totalVolume")])

    print(f"\n[RESULT] {len(results)}/{len(tickers)} passed \"{quote_condition}\":\n")
    print(f"{'TICKER':<8}{'LAST PRICE':>14}{'TOTAL VOLUME':>16}")
    for ticker, last, vol in results:
        last_str = f"{last:.2f}" if isinstance(last, (int, float)) else "-"
        vol_str = f"{vol:,}" if isinstance(vol, (int, float)) else "-"
        print(f"{ticker:<8}{last_str:>14}{vol_str:>16}")

    return results


if __name__ == "__main__":
    main()
