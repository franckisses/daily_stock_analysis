#!/usr/bin/env python3
"""
get_currency.py

Fetch 5 years of exchange-rate data (base USD) for:
    CURRENCIES = ["USD", "CNY", "JPY", "EUR", "HUF"]

- Uses exchangerate.host timeseries API (public, no API key required).
- Computes all pairwise exchange rates for the 5 currencies.
- Produces one PNG chart per currency-pair with time axis and rate curve.
- Annotates each chart with the 5-year high/low (value + date).
- Produces CSV summaries:
    - pair_summary.csv: max/min for each pair
    - currency_summary.csv: best times to exchange USD <-> currency (max/min)
- Requires: requests, matplotlib (standard library only besides these).
- Save charts to ./plots/ and summaries to ./output/

Run:
    python get_currency.py

Author: Generated for user
"""

import os
import sys
from datetime import date
import json
import matplotlib.pyplot as plt
import requests
import pandas as pd
from itertools import combinations
import base64
from io import BytesIO
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders



# --- Configurable constants -------------------------------------------------
CURRENCIES = ["USDCNY", "USDHKD", "USDJPY", "USDEUR", "USDHUF"]
API_BASE = "https://api.exchangerate.host"
PLOTS_DIR = "plots"
OUTPUT_DIR = "output"
PAIR_SUMMARY_CSV = os.path.join(OUTPUT_DIR, "pair_summary.csv")
CURRENCY_SUMMARY_CSV = os.path.join(OUTPUT_DIR, "currency_summary.csv")
# ---------------------------------------------------------------------------


def ensure_dirs():
    os.makedirs(PLOTS_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

def compute_date_range(years=5):
    """Return (start_date_str, end_date_str) in YYYY-MM-DD for the last `years` years.

    Uses a robust method: try replacing the year; if that's invalid (e.g., Feb 29),
    adjust to Feb 28.
    """
    today = date.today()
    try:
        start = today.replace(year=today.year - years)
    except ValueError:
        # Handles Feb 29 -> use Feb 28 of target year
        start = today.replace(year=today.year - years, day=28)
    return start.isoformat(), today.isoformat()


def fetch_timeseries(start_date, end_date, base='USD', symbols=None):
    """Fetch timeseries data from exchangerate.host.

    Returns:
        dict: {date_str: {SYMBOL: rate, ...}, ...}

    Raises:
        RuntimeError if request fails or API returns error.
    """
    api_key = os.environ.get("API_ACCESS_KEY")
    url = f"{API_BASE}/timeframe"
    params = {
        "start_date": start_date,
        "end_date": end_date,
        "source": base,
        "access_key": api_key,
    }
    r = requests.get(url, params=params, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Failed to fetch timeseries: HTTP {r.status_code} {r.text}")
    data = r.json()
    with open("debug_exchange_rate.json", "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=2))
    if not data.get("success", True):
        raise RuntimeError(f"API returned error: {data}")
    rates = data.get("quotes", {})
    print(f"Raw rates data contains {len(rates)} dates.")
    # Ensure base currency present for each date: API sometimes omits base; set USD=1
    filtered = {
        date: {
            pair: value 
            for pair, value in pairs.items() 
            if pair in symbols
        }
        for date, pairs in rates.items()
    }
    return filtered

def construct_mobile_friendly_html(exchange_data):
    """
    构造单列布局、且互为倒数汇率成对出现的 HTML 报告。
    """
    df = pd.DataFrame(exchange_data).T
    # 💡 核心修复：将索引转换为日期时间类型，这样 idxmax() 返回的就是 Timestamp 对象
    df.index = pd.to_datetime(df.index)
    all_currencies = ["USD"] + [c[3:] for c in df.columns]
    # 使用 combinations 获取基础对子（例如 15 对）
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
            .standard { background-color: #f0f7ff; color: #0056b3; } /* 正向颜色 */
            .inverse { background-color: #fff9f0; color: #9a6300; }  /* 倒数颜色 */
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
    print(f"📱 正在构造移动端优化版 HTML (一行一个)...")

    for n1, n2 in base_pairs:
        # 对每一对组合，生成两个方向
        directions = [
            (n1, n2, "Standard", "standard"), # A -> B
            (n2, n1, "Inverse", "inverse")    # B -> A
        ]
        
        for name_from, name_to, label, css_class in directions:
            # 计算汇率
            val1 = df[f"USD{name_from}"] if name_from != "USD" else 1.0
            val2 = df[f"USD{name_to}"] if name_to != "USD" else 1.0
            cross_rate = val2 / val1
            
            # 极值
            max_val, max_date = cross_rate.max(), cross_rate.idxmax()
            min_val, min_date = cross_rate.min(), cross_rate.idxmin()
            current_val = cross_rate.iloc[-1]
            current_date = cross_rate.index[-1]

            # --- ✨ 新增：买入/卖出信号逻辑 ---
            # 计算当前值在区间中的百分比位置 (0% 为最低, 100% 为最高)
            range_span = max_val - min_val
            # 避免除以 0 (如果汇率一直没变)
            position_pct = (current_val - min_val) / range_span if range_span != 0 else 0.5

            # 绘图 (DPI 设为 100 保证清晰度)
            plt.figure(figsize=(10, 5))
            plt.plot(cross_rate.index, cross_rate, color='#3498db' if label=="Standard" else '#e67e22', linewidth=2.5)
            plt.title(f"{name_from} to {name_to}", fontsize=14, fontweight='bold')
            plt.grid(True, linestyle='--', alpha=0.4)
            plt.tight_layout()

            # 转 Base64
            buf = BytesIO()
            plt.savefig(buf, format='png', dpi=100)
            plt.close()
            img_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')

            signal_html = ""
            if position_pct < 0.1: # 处于底部 10% 区间
                signal_html = '<b style="color:#27ae60; background:#eafaf1; padding:2px 6px; border-radius:4px;">📈 BUY SIGNAL (Near Floor)</b>'
            elif position_pct > 0.9: # 处于顶部 10% 区间
                signal_html = '<b style="color:#e74c3c; background:#fdedec; padding:2px 6px; border-radius:4px;">📉 SELL SIGNAL (Near Peak)</b>'
            else:
                signal_html = '<span style="color:#95a5a6;">⚖️ Neutral (Range Bound)</span>'

            # 构造单列卡片
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
                <p>Data Source: Exchangerate API | Generated via GitHub Actions</p>
            </div>
        </div>
    </body>
    </html>
    """
    return html_start + content + html_end

def send_currency_report(html_text):
    # --- 1. 邮件基础配置 ---
    sender_email = os.getenv("EMAIL_SENDER") # 发件人地址
    sender_password = os.getenv("EMAIL_PASSWORD") # 注意：通常是“应用专用密码”，而非登录密码
    smtp_server = "smtp.gmail.com"       # 如 smtp.gmail.com 或 smtp.office365.com
    smtp_port = 587                        # 常用端口：587 (TLS) 或 465 (SSL)

    receivers_raw = os.getenv('EMAIL_RECEIVERS') 
    
    if not sender_email or not sender_password:
        print("❌ 错误：环境变量 EMAIL_SENDER 或 EMAIL_PASSWORD 未设置！")
        return

    # 2. 读取 HTML 文件内容
    try:
        html_body = html_text
    except Exception as e:
        print(f"❌ 读取 HTML 失败: {e}")
        return

    # 3. 构造邮件对象
    # 注意：这里直接使用 MIMEMultipart('alternative') 
    # 这样可以让邮件客户端优先渲染 HTML 部分
    msg = MIMEMultipart('alternative')
    msg['From'] = sender_email
    
    # 整理接收者列表
    receiver_list = [r.strip() for r in receivers_raw.split(',') if r.strip()]
    msg['To'] = ", ".join(receiver_list) if receiver_list else ''
    msg['Subject'] = f"📊 Daily Exchange Rate Report - {pd.Timestamp.now().strftime('%Y-%m-%d')}"

    # 将 HTML 内容作为正文添加
    msg.attach(MIMEText(html_body, 'html'))

    # 4. 执行发送
    try:
        smtp_server = "smtp.gmail.com"
        smtp_port = 587
        
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(sender_email, sender_password)
        server.send_message(msg)
        server.quit()
        
        print(f"🚀 报告已成功作为【邮件正文】发送至: {msg['To']}")
        
    except Exception as e:
        print(f"❌ 发送失败: {e}")


def main():
    print("Starting fetch and plot of currency rates (past 1 year)...")
    ensure_dirs()
    years = int(os.getenv("DATE_RANGE", 1))
    start_date, end_date = compute_date_range(years=years)
    print(f"Date range: {start_date} -> {end_date}")

    # Fetch timeseries (base USD)
    print("Fetching timeseries from exchangerate.host ...")
    rates = fetch_timeseries(start_date, end_date,base='USD', symbols=CURRENCIES)
    # generate the html report with paired charts and annotations
    send_currency_report(construct_mobile_friendly_html(rates))

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("Error:", exc)
        sys.exit(1)
