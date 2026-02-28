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


def generate_aligned_report(exchange_data, output_file='paired_currency_report.html'):
    """
    生成一个每行显示一对互为倒数汇率的 HTML 报告。
    """
    # 1. 数据准备
    df = pd.DataFrame(exchange_data).T
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    
    all_currencies = ["USD"] + [c[3:] for c in df.columns]
    # 使用 combinations 获取 15 组，然后在循环中手动生成它们的 A->B 和 B->A
    base_pairs = list(combinations(all_currencies, 2))
    
    html_content = """
    <html>
    <head>
        <style>
            body { font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; background-color: #f0f2f5; margin: 40px; }
            h1 { color: #1a3a5f; text-align: center; margin-bottom: 10px; }
            .subtitle { text-align: center; color: #666; margin-bottom: 40px; }
            .row { display: flex; justify-content: space-between; margin-bottom: 30px; gap: 20px; }
            .card { background: white; border-radius: 12px; box-shadow: 0 2px 10px rgba(0,0,0,0.08); padding: 20px; width: 48%; border-top: 5px solid #3498db; }
            .card.reverse { border-top: 5px solid #e67e22; } /* 反向汇率用不同颜色区分 */
            .card h3 { margin: 0 0 15px 0; font-size: 1.2em; color: #2c3e50; display: flex; justify-content: space-between; }
            .badge { font-size: 0.7em; padding: 4px 8px; border-radius: 4px; background: #eee; }
            img { width: 100%; height: auto; border-radius: 4px; }
            .footer { text-align: center; padding: 40px; color: #95a5a6; font-size: 0.8em; }
        </style>
    </head>
    <body>
        <h1>📊 Bidirectional Exchange Rate Matrix</h1>
        <p class="subtitle">Full analysis of 15 pairs (30 total directions) | Paired for easy comparison</p>
    """

    print(f"🔄 正在按配对逻辑处理 15 组对称汇率...")

    # 2. 循环处理每一对组合
    for name1, name2 in base_pairs:
        html_content += '<div class="row">'
        
        # 定义两个方向：A->B 和 B->A
        directions = [(name1, name2, "#3498db", "Standard"), (name2, name1, "#e67e22", "Inverse")]
        
        for n1, n2, color, label in directions:
            # 计算汇率
            val1 = df[f"USD{n1}"] if n1 != "USD" else 1.0
            val2 = df[f"USD{n2}"] if n2 != "USD" else 1.0
            cross_rate = val2 / val1
            
            # 绘图
            plt.figure(figsize=(8, 5))
            plt.plot(cross_rate.index, cross_rate, color=color, marker='o', linewidth=2)
            
            # 极值标注
            max_val, max_date = cross_rate.max(), cross_rate.idxmax()
            min_val, min_date = cross_rate.min(), cross_rate.idxmin()
            plt.annotate(f'Peak: {max_val:.4f}', xy=(max_date, max_val), xytext=(5,5), textcoords='offset points', color='red', weight='bold', size=9)
            plt.annotate(f'Floor: {min_val:.4f}', xy=(min_date, min_val), xytext=(5,-15), textcoords='offset points', color='green', weight='bold', size=9)
            
            plt.title(f'{n1} to {n2}', color='#34495e')
            plt.xlabel('Date')
            plt.grid(True, alpha=0.2)
            plt.tight_layout()

            # 转 Base64
            buf = BytesIO()
            plt.savefig(buf, format='png', dpi=100) # 稍微降低 dpi 减小 HTML 体积
            plt.close()
            img_str = base64.b64encode(buf.getvalue()).decode('utf-8')
            
            # 生成卡片
            card_class = "card" if label == "Standard" else "card reverse"
            html_content += f"""
                <div class="{card_class}">
                    <h3>{n1} ➜ {n2} <span class="badge">{label}</span></h3>
                    <img src="data:image/png;base64,{img_str}" />
                </div>
            """
        
        html_content += '</div>' # 结束当前行

    html_content += """
        <div class="footer">
            <p>End of Task | Data Grounding: 2026 Financial Archive</p>
        </div>
    </body>
    </html>
    """

    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(html_content)
    
    print(f"✨ 对称式报告已生成: {output_file}")



def send_currency_report(html_file, receiver_email):
    # --- 1. 邮件基础配置 ---
    sender_email = os.getenv("EMAIL_SENDER", "")
    sender_password = os.getenv("EMAIL_PASSWORD", "") # 注意：通常是“应用专用密码”，而非登录密码
    smtp_server = "smtp.gmail.com"       # 如 smtp.gmail.com 或 smtp.office365.com
    smtp_port = 587                        # 常用端口：587 (TLS) 或 465 (SSL)

    # 创建邮件容器
    msg = MIMEMultipart()
    msg['From'] = sender_email
    msg['To'] = os.getenv("EMAIL_RECEIVERS") # 可以是逗号分隔的多个地址
    msg['Subject'] = f"📊 Daily Exchange Rate Analysis Report - {pd.Timestamp.now().strftime('%Y-%m-%d')}"

    # --- 2. 邮件正文 (简短导语) ---
    body = """
    Hi Team,

    Please find the comprehensive exchange rate analysis report attached. 
    The report covers 30 bidirectional currency pairs with peak and floor annotations.

    Best regards,
    Automated Financial Reporter
    """
    msg.attach(MIMEText(body, 'plain'))

    # --- 3. 读取并添加 HTML 附件 ---
    try:
        with open(html_file, "rb") as attachment:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(attachment.read())
        
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            f"attachment; filename= {os.path.basename(html_file)}",
        )
        msg.attach(part)
        
        # --- 4. 发送邮件 ---
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()  # 启用安全传输
        server.login(sender_email, sender_password)
        server.send_message(msg)
        server.quit()
        
        print(f"🚀 报告已成功发送至: {receiver_email}")
        
    except Exception as e:
        print(f"❌ 发送失败: {e}")


def main():
    print("Starting fetch and plot of currency rates (past 1 year)...")
    ensure_dirs()
    start_date, end_date = compute_date_range(years=1)
    print(f"Date range: {start_date} -> {end_date}")

    # Fetch timeseries (base USD)
    print("Fetching timeseries from exchangerate.host ...")
    rates = fetch_timeseries(start_date, end_date,base='USD', symbols=CURRENCIES)
    # generate the html report with paired charts
    generate_aligned_report(rates)
    send_currency_report('paired_currency_report.html', 'boss@example.com')

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("Error:", exc)
        sys.exit(1)
