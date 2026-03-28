#!/usr/bin/env python3
"""
get_currency.py

Currency report pipeline:
1) Persist FX data into local SQLite (initialize 365 days, then daily incremental updates)
2) Read historical data from SQLite
3) Render paired reciprocal charts into HTML
4) Email HTML report
"""

import base64
import json
import os
import smtplib
import sqlite3
import sys
from datetime import date, datetime, timedelta
from io import BytesIO
from itertools import combinations

import matplotlib.pyplot as plt
import pandas as pd
import requests
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

try:
    from dotenv import load_dotenv

    load_dotenv()
except ModuleNotFoundError:
    # In CI, env vars are injected directly. dotenv is optional.
    pass


# --- Configurable constants -------------------------------------------------
BASE_CURRENCY = "USD"
QUOTE_CURRENCIES = ["CNY", "HKD", "JPY", "EUR", "HUF"]
PAIR_CODES = [f"{BASE_CURRENCY}{quote}" for quote in QUOTE_CURRENCIES]

API_BASE = "https://api.exchangerate.host"
PLOTS_DIR = "plots"
OUTPUT_DIR = "output"
REPORT_HTML_PATH = "paired_currency_report.html"

DB_PATH = os.getenv("FX_DB_PATH", "data/fx_data.db")
FX_TABLE = "fx_daily"
# ---------------------------------------------------------------------------


def read_int_env(name, default):
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"Invalid integer in env {name}={raw!r}, fallback to {default}.")
        return default


INIT_DAYS = read_int_env("FX_INIT_DAYS", 365)


def ensure_dirs():
    os.makedirs(PLOTS_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)


def compute_date_range(years=5):
    """Return (start_date_str, end_date_str) in YYYY-MM-DD for the last `years` years."""
    today = date.today()
    try:
        start = today.replace(year=today.year - years)
    except ValueError:
        # Handles Feb 29 -> use Feb 28 of target year
        start = today.replace(year=today.year - years, day=28)
    return start.isoformat(), today.isoformat()


def init_db(conn):
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {FX_TABLE} (
            base TEXT NOT NULL,
            quote TEXT NOT NULL,
            date TEXT NOT NULL,
            rate REAL NOT NULL,
            source TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (base, quote, date)
        )
        """
    )
    conn.commit()


def get_db_max_date(conn):
    cursor = conn.execute(f"SELECT MAX(date) FROM {FX_TABLE} WHERE base = ?", (BASE_CURRENCY,))
    row = cursor.fetchone()
    return row[0] if row and row[0] else None


def fetch_timeseries(start_date, end_date, base=BASE_CURRENCY, symbols=None):
    """Fetch timeseries data from exchangerate.host-compatible timeframe API."""
    api_key = os.getenv("API_ACCESS_KEY")
    print(api_key,'-'*20)
    url = f"{API_BASE}/timeframe"
    params = {
        "start_date": start_date,
        "end_date": end_date,
        "source": base,
        "access_key": api_key,
    }
    response = requests.get(url, params=params, timeout=30)
    if not response.ok:
        raise RuntimeError(
            f"Failed to fetch timeseries: HTTP {response.status_code} {response.text}"
        )

    data = response.json()
    # with open("debug_exchange_rate.json", "w", encoding="utf-8") as f:
    #     f.write(json.dumps(data, indent=2, ensure_ascii=False))

    if not data.get("success", True):
        raise RuntimeError(f"API returned error: {data}")

    quotes = data.get("quotes", {})
    filtered = {
        date_key: {
            pair: value for pair, value in pairs.items() if (not symbols or pair in symbols)
        }
        for date_key, pairs in quotes.items()
    }
    return filtered


def upsert_rates_to_db(conn, rates_by_date, source="exchangerate.host"):
    now_utc = datetime.utcnow().isoformat(timespec="seconds")
    rows = []

    for day, pair_map in rates_by_date.items():
        for pair_code, rate in pair_map.items():
            if not pair_code.startswith(BASE_CURRENCY):
                continue
            quote = pair_code[len(BASE_CURRENCY) :]
            rows.append((BASE_CURRENCY, quote, day, float(rate), source, now_utc))

    if not rows:
        return 0

    conn.executemany(
        f"""
        INSERT INTO {FX_TABLE}(base, quote, date, rate, source, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(base, quote, date)
        DO UPDATE SET
            rate = excluded.rate,
            source = excluded.source,
            updated_at = excluded.updated_at
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def determine_incremental_range(conn):
    today = date.today()
    max_date = get_db_max_date(conn)

    if not max_date:
        start = today - timedelta(days=INIT_DAYS)
        return start.isoformat(), today.isoformat(), "initialize"

    start = datetime.strptime(max_date, "%Y-%m-%d").date() + timedelta(days=1)
    if start > today:
        return None, None, "up_to_date"
    return start.isoformat(), today.isoformat(), "incremental"


def update_offline_database(conn):
    start_date, end_date, mode = determine_incremental_range(conn)

    if mode == "up_to_date":
        print("SQLite data is already up-to-date.")
        return 0

    print(f"Updating SQLite ({mode}): {start_date} -> {end_date}")
    rates = fetch_timeseries(start_date, end_date, base=BASE_CURRENCY, symbols=PAIR_CODES)
    inserted_or_updated = upsert_rates_to_db(conn, rates)
    print(f"SQLite rows inserted/updated: {inserted_or_updated}")
    return inserted_or_updated


def load_rates_from_db(conn, report_years=None):
    params = [BASE_CURRENCY]
    sql = f"""
        SELECT date, quote, rate
        FROM {FX_TABLE}
        WHERE base = ?
    """

    if report_years:
        start_date, _ = compute_date_range(years=report_years)
        sql += " AND date >= ?"
        params.append(start_date)

    sql += " ORDER BY date ASC"

    rows = conn.execute(sql, params).fetchall()
    exchange_data = {}
    for day, quote, rate in rows:
        pair_code = f"{BASE_CURRENCY}{quote}"
        exchange_data.setdefault(day, {})[pair_code] = rate

    return exchange_data


def log_db_health(conn):
    latest_date_row = conn.execute(
        f"SELECT MAX(date) FROM {FX_TABLE} WHERE base = ?",
        (BASE_CURRENCY,),
    ).fetchone()
    latest_date = latest_date_row[0] if latest_date_row else None

    total_count_row = conn.execute(
        f"SELECT COUNT(*) FROM {FX_TABLE} WHERE base = ?",
        (BASE_CURRENCY,),
    ).fetchone()
    total_count = total_count_row[0] if total_count_row else 0

    by_quote_rows = conn.execute(
        f"""
        SELECT quote, COUNT(*) AS cnt
        FROM {FX_TABLE}
        WHERE base = ?
        GROUP BY quote
        ORDER BY quote
        """,
        (BASE_CURRENCY,),
    ).fetchall()

    print("=== SQLite Health Check ===")
    print(f"DB Path: {DB_PATH}")
    print(f"Base Currency: {BASE_CURRENCY}")
    print(f"Latest Date: {latest_date if latest_date else 'N/A'}")
    print(f"Total Rows: {total_count}")
    print("Rows By Quote:")
    if not by_quote_rows:
        print("  (no rows)")
    for quote, cnt in by_quote_rows:
        print(f"  {BASE_CURRENCY}{quote}: {cnt}")
    print("===========================")


def construct_mobile_friendly_html(exchange_data):
    """构造单列布局、且互为倒数汇率成对出现的 HTML 报告。"""
    if not exchange_data:
        raise RuntimeError("No exchange data available in SQLite for reporting.")

    df = pd.DataFrame(exchange_data).T
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()

    all_currencies = [BASE_CURRENCY] + [c[len(BASE_CURRENCY) :] for c in df.columns]
    base_pairs = list(combinations(all_currencies, 2))

    html_start = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
                   background-color: #f0f2f5; margin: 0; padding: 10px; }
            .header { background: linear-gradient(135deg, #1a3a5f 0%, #2c3e50 100%); color: white;
                      padding: 25px 15px; text-align: center; border-radius: 12px; margin-bottom: 20px; }
            .container { max-width: 800px; margin: 0 auto; }
            .card { background: #ffffff; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.08);
                    margin-bottom: 25px; overflow: hidden; border: 1px solid #e1e4e8; }
            .card-header { padding: 12px 15px; font-weight: bold; font-size: 16px; border-bottom: 1px solid #f0f0f0; }
            .standard { background-color: #f0f7ff; color: #0056b3; }
            .inverse { background-color: #fff9f0; color: #9a6300; }
            img { width: 100%; height: auto; display: block; }
            .info { padding: 12px 15px; font-size: 13px; color: #666; background: #fafafa; }
            .footer { text-align: center; padding: 30px; color: #999; font-size: 12px; }
            b { color: #333; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1 style="margin:0; font-size: 22px;">Currency Matrix Analysis</h1>
                <p style="margin:10px 0 0 0; opacity: 0.8;">Paired Reciprocal Rates (30 Directions)</p>
            </div>
    """

    content = ""
    print("📱 正在构造移动端优化版 HTML (一行一个)...")

    for n1, n2 in base_pairs:
        directions = [
            (n1, n2, "Standard", "standard"),
            (n2, n1, "Inverse", "inverse"),
        ]

        for name_from, name_to, label, css_class in directions:
            val1 = df[f"{BASE_CURRENCY}{name_from}"] if name_from != BASE_CURRENCY else 1.0
            val2 = df[f"{BASE_CURRENCY}{name_to}"] if name_to != BASE_CURRENCY else 1.0
            cross_rate = (val2 / val1).dropna()
            if cross_rate.empty:
                continue

            max_val, max_date = cross_rate.max(), cross_rate.idxmax()
            min_val, min_date = cross_rate.min(), cross_rate.idxmin()
            current_val = cross_rate.iloc[-1]
            current_date = cross_rate.index[-1]

            range_span = max_val - min_val
            position_pct = (current_val - min_val) / range_span if range_span != 0 else 0.5

            fig, ax = plt.subplots(figsize=(10, 5))
            ax.plot(
                cross_rate.index,
                cross_rate,
                color="#3498db" if label == "Standard" else "#e67e22",
                linewidth=2.5,
            )
            ax.set_title(f"{name_from} to {name_to}", fontsize=14, fontweight="bold")
            ax.grid(True, linestyle="--", alpha=0.4)
            # Keep enough left padding so Y-axis labels are not clipped on mobile.
            fig.subplots_adjust(left=0.16, right=0.98, bottom=0.12, top=0.9)

            buf = BytesIO()
            fig.savefig(buf, format="png", dpi=110, bbox_inches="tight", pad_inches=0.2)
            plt.close(fig)
            img_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

            if position_pct < 0.1:
                signal_html = (
                    '<b style="color:#27ae60; background:#eafaf1; padding:2px 6px; border-radius:4px;">'
                    "📈 BUY SIGNAL (Near Floor)</b>"
                )
            elif position_pct > 0.9:
                signal_html = (
                    '<b style="color:#e74c3c; background:#fdedec; padding:2px 6px; border-radius:4px;">'
                    "📉 SELL SIGNAL (Near Peak)</b>"
                )
            else:
                signal_html = '<span style="color:#95a5a6;">⚖️ Neutral (Range Bound)</span>'

            content += f"""
            <div class="card">
                <div class="card-header {css_class}">
                    {name_from} ➜ {name_to} ({label})
                </div>
                <img src="data:image/png;base64,{img_b64}">
                <div class="info">
                    <b>Peak:</b> {max_val:.4f} <span style="color:#999;">({max_date.strftime('%Y-%m-%d')})</span><br>
                    <b>Floor:</b> {min_val:.4f} <span style="color:#999;">({min_date.strftime('%Y-%m-%d')})</span><br>
                    <b style="color:#2980b9;">Current:</b> {current_val:.4f} <span style="color:#999;">({current_date.strftime('%Y-%m-%d')})</span><br>
                    <div style="margin-top:8px;">{signal_html}</div>
                </div>
            </div>
            """

    html_end = """
            <div class="footer">
                <p>Data Source: Exchangerate API | Storage: SQLite | Generated via GitHub Actions</p>
            </div>
        </div>
    </body>
    </html>
    """
    return html_start + content + html_end


def write_report_html(html_text):
    with open(REPORT_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html_text)
    print(f"HTML report written: {REPORT_HTML_PATH}")


def send_currency_report(html_text):
    sender_email = os.getenv("EMAIL_SENDER")
    sender_password = os.getenv("EMAIL_PASSWORD")
    receivers_raw = os.getenv("EMAIL_RECEIVERS", "")

    if not sender_email or not sender_password:
        print("❌ 跳过邮件发送：环境变量 EMAIL_SENDER 或 EMAIL_PASSWORD 未设置。")
        return

    receiver_list = [r.strip() for r in receivers_raw.split(",") if r.strip()]
    if not receiver_list:
        print("❌ 跳过邮件发送：EMAIL_RECEIVERS 未设置有效收件人。")
        return

    msg = MIMEMultipart("alternative")
    msg["From"] = sender_email
    msg["To"] = ", ".join(receiver_list)
    msg["Subject"] = f"📊 Daily Exchange Rate Report - {pd.Timestamp.now().strftime('%Y-%m-%d')}"
    msg.attach(MIMEText(html_text, "html"))

    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(sender_email, sender_password)
        server.send_message(msg)
        server.quit()
        print(f"🚀 报告已成功作为【邮件正文】发送至: {msg['To']}")
    except Exception as e:
        print(f"❌ 发送失败: {e}")


def main():
    print("Starting currency pipeline with SQLite persistence...")
    ensure_dirs()

    report_years = read_int_env("DATE_RANGE", 0)

    conn = sqlite3.connect(DB_PATH)
    try:
        init_db(conn)
        update_offline_database(conn)
        log_db_health(conn)

        rates = load_rates_from_db(conn, report_years=report_years if report_years > 0 else None)
        if not rates:
            raise RuntimeError("No rate records loaded from SQLite. Cannot generate report.")

        html = construct_mobile_friendly_html(rates)
        write_report_html(html)
        send_currency_report(html)
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("Error:", exc)
        sys.exit(1)
