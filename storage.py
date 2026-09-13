"""Build the local Finviz fundamentals cache used by screener.py."""

from __future__ import annotations

import argparse
import csv
import time
from pathlib import Path

import requests
from lxml import html as lxml_html

from screener import (
    FINVIZ_BATCH_SIZE,
    FINVIZ_COOLDOWN_SEC,
    FINVIZ_PAGE_DELAY_SEC,
    _TICKER_LINK_RE,
    _dump_debug_html,
    _extract_total_rows,
    _fetch_finviz_page,
)

# Finviz custom-view column IDs. Keep these aligned with finvizfinance.
CUSTOM_SCREENER_COLUMNS = {
    0: "No.", 1: "Ticker", 2: "Company", 3: "Sector", 4: "Industry",
    5: "Country", 6: "Market Cap.", 7: "P/E", 8: "Forward P/E", 9: "PEG",
    10: "P/S", 11: "P/B", 12: "P/Cash", 13: "P/Free Cash Flow",
    14: "Dividend Yield", 15: "Payout Ratio", 16: "EPS",
    17: "EPS growth this year", 18: "EPS growth next year",
    19: "EPS growth past 5 years", 20: "EPS growth next 5 years",
    21: "Sales growth past 5 years", 22: "EPS growth qtr over qtr",
    23: "Sales growth qtr over qtr", 24: "Shares Outstanding",
    25: "Shares Float", 26: "Insider Ownership", 27: "Insider Transactions",
    28: "Institutional Ownership", 29: "Institutional Transactions",
    30: "Float Short", 31: "Short Ratio", 32: "Return on Assets",
    33: "Return on Equity", 34: "Return on Investments",
    35: "Current Ratio", 36: "Quick Ratio", 37: "Long Term Debt/Equity",
    38: "Total Debt/Equity", 39: "Gross Margin", 40: "Operating Margin",
    41: "Net Profit Margin", 42: "Performance (Week)",
    43: "Performance (Month)", 44: "Performance (Quarter)",
    45: "Performance (Half Year)", 46: "Performance (Year)",
    47: "Performance (YearToDate)", 48: "Beta", 49: "Average True Range",
    50: "Volatility (Week)", 51: "Volatility (Month)",
    52: "20-Day Simple Moving Average", 53: "50-Day Simple Moving Average",
    54: "200-Day Simple Moving Average", 55: "50-Day High",
    56: "50-Day Low", 57: "52-Week High", 58: "52-Week Low", 59: "RSI",
    60: "Change from Open", 61: "Gap", 62: "Analyst Recom.",
    63: "Average Volume", 64: "Relative Volume", 65: "Price", 66: "Change",
    67: "Volume", 68: "Earnings Date", 69: "Target Price", 70: "IPO Date",
    73: "Book value per share", 74: "Cash per share", 75: "Dividend",
    76: "Employees", 77: "EPS estimate next quarter", 78: "Income",
    79: "Index", 80: "Optionable", 81: "Previous Close", 82: "Sales",
    83: "Shortable", 84: "Short Interest", 85: "Float/Outstanding",
    86: "Open", 87: "High", 88: "Low",
}

# Pull all fields in one custom-view request instead of scraping each tab.
REQUESTED_COLUMN_IDS = [
    0, 1, 2, 3, 4, 5,                              # Overview: No./Ticker/Company/Sector/Industry/Country
    6, 7, 8, 9, 10, 11, 12, 13,                    # Valuation: Market Cap/P/E/Fwd P/E/PEG/P/S/P/B/P/Cash/P/FCF
    24, 25, 26, 27, 28, 29, 30, 31,                # Ownership: Shares Out/Float/Insider Own+Trans/Inst Own+Trans/Float Short/Short Ratio
    32, 33, 34, 35, 36, 37, 38, 39, 40, 41,        # Financial: ROA/ROE/ROI/Current+Quick Ratio/LT+Total Debt-Equity/Margins
    42, 43, 44, 45, 46, 47, 63, 64,                # Performance: Perf Week..YTD, Avg Volume, Relative Volume
    48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59,  # Technical: Beta/ATR/Volatility/SMA20-50-200/50D+52W High-Low/RSI
    60, 61, 62,                                    # Change from Open, Gap, Analyst Recom.
    65, 66, 67,                                    # Price, Change, Volume
]

CUSTOM_VIEW_CODE = "151"


def _custom_view_url(column_ids: list[int]) -> str:
    """Build a Finviz custom-view URL."""
    ids = [c for c in column_ids if c != 0]
    ids = [0] + ids
    ids_str = ",".join(str(i) for i in ids)
    return f"https://finviz.com/screener.ashx?v={CUSTOM_VIEW_CODE}&c={ids_str}&o=ticker"


def _parse_screener_table(html_text: str) -> tuple[list[str], list[dict]]:
    """Parse the header and ticker rows from one screener page."""
    tree = lxml_html.fromstring(html_text)
    trs = tree.xpath("//tr")

    header_idx = None
    headers: list[str] = []
    for i, tr in enumerate(trs):
        cells = tr.xpath("./td | ./th")
        texts = [c.text_content().strip() for c in cells]
        if "Ticker" in texts:
            header_idx = i
            headers = texts
            break

    if header_idx is None:
        return [], []

    rows = []
    for tr in trs[header_idx + 1:]:
        row_html = lxml_html.tostring(tr, encoding="unicode")
        m = _TICKER_LINK_RE.search(row_html)
        if not m:
            continue
        ticker = m.group(1).upper()
        values = [c.text_content().strip() for c in tr.xpath("./td")]
        if headers and len(values) == len(headers):
            row = dict(zip(headers, values))
        else:
            row = {"_raw_cells": values}
        row["Ticker"] = ticker
        rows.append(row)
    return headers, rows


def fetch_all_fundamentals(column_ids: list[int]) -> dict:
    """Fetch every page of the unfiltered custom view."""
    url = _custom_view_url(column_ids)
    session = requests.Session()
    try:
        first_response = _fetch_finviz_page(session, url)
    except requests.RequestException as exc:
        raise RuntimeError(f"Could not reach Finviz for {url}: {exc}") from exc

    headers, first_rows = _parse_screener_table(first_response.text)
    if not first_rows:
        _dump_debug_html(url, first_response)
        raise RuntimeError("First page contained no data rows.")

    print(f"  [CUSTOM] header columns detected: {headers}")
    print(f"  [CUSTOM] sample row: {first_rows[0]}")

    total_rows = _extract_total_rows(first_response.text) or len(first_rows)
    page_urls = [url] + [f"{url}&r={start}" for start in range(21, total_rows + 1, 20)]
    print(f"  [CUSTOM] {total_rows} total rows across {len(page_urls)} pages -- "
          f"fetching all in paced batches.")

    out: dict = {}
    for i, page_url in enumerate(page_urls, start=1):
        if i > 1:
            time.sleep(FINVIZ_PAGE_DELAY_SEC)

        response = first_response if i == 1 else None
        try:
            if response is None:
                response = _fetch_finviz_page(session, page_url)
            _, page_rows = _parse_screener_table(response.text)
            if not page_rows:
                raise RuntimeError("page returned no data rows")
        except Exception as e:
            print(f"  [WARN] Custom-view page {i} failed ({e}) -- "
                  f"cooling down {FINVIZ_COOLDOWN_SEC}s and retrying once.")
            time.sleep(FINVIZ_COOLDOWN_SEC)
            try:
                response = _fetch_finviz_page(session, page_url)
                _, page_rows = _parse_screener_table(response.text)
                if not page_rows:
                    raise RuntimeError("page returned no data rows")
            except Exception as e2:
                print(f"  [WARN] Page {i} failed again ({e2}) -- "
                      f"stopping pagination early with {len(out)}/{total_rows} rows collected.")
                if response is not None:
                    _dump_debug_html(page_url, response)
                break

        new_on_page = 0
        for row in page_rows:
            ticker = row.get("Ticker")
            if ticker and ticker not in out:
                out[ticker] = row
                new_on_page += 1
        print(f"  [CUSTOM] page {i}/{len(page_urls)}: {len(page_rows)} rows on page, "
              f"{new_on_page} new (running total {len(out)}/{total_rows})")

        if i % FINVIZ_BATCH_SIZE == 0 and i < len(page_urls):
            time.sleep(FINVIZ_COOLDOWN_SEC)

    if len(out) < total_rows:
        print(f"  [WARNING] collected {len(out)}/{total_rows} -- "
              f"some pages came back short or failed. See [WARN] lines above.")

    return out


def build_fundamentals_csv(out_path: Path) -> int:
    """Fetch the fundamentals universe and write it to CSV."""
    print(f"[STORAGE] Fetching Custom view (v={CUSTOM_VIEW_CODE}) -- "
          f"{len(REQUESTED_COLUMN_IDS)} columns spanning Overview/Valuation/"
          f"Financial/Ownership/Performance/Technical in ONE scrape...")
    rows = fetch_all_fundamentals(REQUESTED_COLUMN_IDS)

    fieldnames: list[str] = []
    for row in rows.values():
        for k in row.keys():
            if k not in fieldnames:
                fieldnames.append(k)
        break

    if not fieldnames:
        raise RuntimeError("No columns detected -- nothing to write. "
                            "Check the '[CUSTOM] header columns detected' line above.")

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, restval="")
        writer.writeheader()
        for ticker in sorted(rows):
            writer.writerow(rows[ticker])

    print(f"[STORAGE] Wrote {len(rows)} rows to {out_path.resolve()}")
    return len(rows)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="fundamentals.csv",
                         help="Output CSV path (default: fundamentals.csv)")
    args = parser.parse_args()
    build_fundamentals_csv(Path(args.out))


if __name__ == "__main__":
    main()
