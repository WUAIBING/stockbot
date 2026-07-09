"""V10 Strategy Email Notification Helper

Sends formatted buy/sell signal emails via QQ Mail SMTP.
Called by automations at 14:30 (pre-warm), 14:50 (decision), and after close (review).

Usage:
    python send_email.py --type prewarm|decision|review [--csv PATH]
"""

import smtplib
import argparse
import csv
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

from package_paths import DATA_DIR

SMTP_SERVER = "smtp.qq.com"
SMTP_PORT = 465
SENDER = "1656147660@qq.com"
RECEIVER = "1656147660@qq.com"
AUTH_CODE = "erbapufltcwfbgdj"

DEFAULT_CSV = str(DATA_DIR / "v10_scan_full.csv")


def load_signals(csv_path):
    """Load scan results, return dict {tier: [row_dict, ...]}"""
    signals = {1: [], 2: [], 3: []}
    if not os.path.exists(csv_path):
        return signals
    with open(csv_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            tier = int(row.get("tier", 0))
            if tier in signals:
                signals[tier].append(row)
    # Sort each tier by weekly_slope desc
    for t in signals:
        signals[t].sort(key=lambda r: float(r.get("weekly_slope", 0)), reverse=True)
    return signals


def fmt_price(v):
    try:
        return f"{float(v):.2f}"
    except (ValueError, TypeError):
        return str(v)


def fmt_pct(v):
    try:
        return f"{float(v):+.1f}%"
    except (ValueError, TypeError):
        return str(v)


def build_prewarm_html(signals, date_str):
    t1 = signals[1]
    t2 = signals[2][:5]
    t3 = signals[3][:3]
    total = len(signals[1]) + len(signals[2]) + len(signals[3])

    t1_rows = ""
    for s in t1:
        t1_rows += f'<tr><td>{s["code"]}</td><td>{s["name"]}</td><td>{fmt_price(s["entry_price"])}</td><td>100%</td></tr>'
    if not t1:
        t1_rows = '<tr><td colspan="4" style="color:#999;text-align:center;">今日无T1信号</td></tr>'

    t2_rows = ""
    for s in t2:
        pos = s.get("position", "60%")
        t2_rows += f'<tr><td>{s["code"]}</td><td>{s["name"]}</td><td>{fmt_price(s["entry_price"])}</td><td>{pos}</td></tr>'
    if not t2:
        t2_rows = '<tr><td colspan="4" style="color:#999;text-align:center;">无</td></tr>'

    t3_rows = ""
    for s in t3:
        t3_rows += f'<tr><td>{s["code"]}</td><td>{s["name"]}</td><td>{fmt_price(s["entry_price"])}</td><td>30%</td></tr>'
    if not t3:
        t3_rows = '<tr><td colspan="4" style="color:#999;text-align:center;">无</td></tr>'

    html = f"""<html><body style="font-family:Arial,sans-serif;color:#333;max-width:480px;">
<h2 style="color:#3498db;margin-bottom:4px;"> V10预热扫描</h2>
<p style="color:#888;font-size:13px;margin-top:0;">{date_str} 14:30</p>

<h3 style="color:#e74c3c;font-size:15px;margin-bottom:6px;">T1 大肉 · 仓位100%</h3>
<table cellpadding="5" cellspacing="0" style="border-collapse:collapse;width:100%;font-size:14px;">
<tr style="background:#f8f9fa;font-weight:bold;"><td>代码</td><td>名称</td><td>价格</td><td>仓位</td></tr>
{t1_rows}</table>

<h3 style="color:#f39c12;font-size:15px;margin-bottom:6px;">T2 中肉 · Top5推荐</h3>
<table cellpadding="5" cellspacing="0" style="border-collapse:collapse;width:100%;font-size:14px;">
<tr style="background:#f8f9fa;font-weight:bold;"><td>代码</td><td>名称</td><td>价格</td><td>仓位</td></tr>
{t2_rows}</table>

<h3 style="color:#27ae60;font-size:15px;margin-bottom:6px;">T3 小肉 · Top3推荐</h3>
<table cellpadding="5" cellspacing="0" style="border-collapse:collapse;width:100%;font-size:14px;">
<tr style="background:#f8f9fa;font-weight:bold;"><td>代码</td><td>名称</td><td>价格</td><td>仓位</td></tr>
{t3_rows}</table>

<p style="font-size:14px;margin-top:12px;"> 共 <b>{total}</b> 个信号 | T1={len(t1)} T2={len(signals[2])} T3={len(signals[3])}</p>
<p style="font-size:13px;color:#888;">14:50 将发送最终决策</p>
</body></html>"""
    return html


def build_decision_html(signals, date_str):
    t1 = signals[1]
    t2 = signals[2][:5]
    t3 = signals[3][:3]
    total = len(signals[1]) + len(signals[2]) + len(signals[3])

    # Position sizing suggestion
    n_t1, n_t2, n_t3 = len(signals[1]), len(signals[2]), len(signals[3])
    if n_t1 >= 2:
        pos_pct = "90%"
    elif n_t1 >= 1:
        pos_pct = "80%"
    elif n_t2 >= 5:
        pos_pct = "65%"
    elif n_t2 >= 2:
        pos_pct = "50%"
    elif n_t3 >= 5:
        pos_pct = "35%"
    else:
        pos_pct = "20%"

    rows = ""
    for s in t1:
        rows += f'<tr><td>{s["code"]}</td><td>{s["name"]}</td><td>{fmt_price(s["entry_price"])}</td></tr>'
    if not t1:
        rows = '<tr><td colspan="3" style="color:#999;text-align:center;">今日无T1信号</td></tr>'

    t2_rows = ""
    for s in t2:
        t2_rows += f'<tr><td>{s["code"]}</td><td>{s["name"]}</td><td>{fmt_price(s["entry_price"])}</td></tr>'
    if not t2:
        t2_rows = '<tr><td colspan="3" style="color:#999;text-align:center;">无</td></tr>'

    t3_rows = ""
    for s in t3:
        t3_rows += f'<tr><td>{s["code"]}</td><td>{s["name"]}</td><td>{fmt_price(s["entry_price"])}</td></tr>'
    if not t3:
        t3_rows = '<tr><td colspan="3" style="color:#999;text-align:center;">无</td></tr>'

    html = f"""<html><body style="font-family:Arial,sans-serif;color:#333;max-width:480px;">
<h2 style="color:#e74c3c;margin-bottom:4px;"> V10决策 · 买入清单</h2>
<p style="color:#888;font-size:13px;margin-top:0;">{date_str} 14:50 · 14:55市价执行</p>

<h3 style="color:#e74c3c;font-size:15px;margin-bottom:6px;">T1 大肉 · 100%仓位 · 必买</h3>
<table cellpadding="5" cellspacing="0" style="border-collapse:collapse;width:100%;font-size:14px;">
<tr style="background:#f8f9fa;font-weight:bold;"><td>代码</td><td>名称</td><td>价格</td></tr>
{rows}</table>

<h3 style="color:#f39c12;font-size:15px;margin-bottom:6px;">T2 中肉 · 50-60%仓位 · 推荐Top5</h3>
<table cellpadding="5" cellspacing="0" style="border-collapse:collapse;width:100%;font-size:14px;">
<tr style="background:#f8f9fa;font-weight:bold;"><td>代码</td><td>名称</td><td>价格</td></tr>
{t2_rows}</table>

<h3 style="color:#27ae60;font-size:15px;margin-bottom:6px;">T3 小肉 · 30%仓位 · 推荐Top3</h3>
<table cellpadding="5" cellspacing="0" style="border-collapse:collapse;width:100%;font-size:14px;">
<tr style="background:#f8f9fa;font-weight:bold;"><td>代码</td><td>名称</td><td>价格</td></tr>
{t3_rows}</table>

<p style="font-size:15px;margin-top:12px;"> 建议总仓位: <b style="color:#e74c3c;">{pos_pct}</b></p>
<p style="font-size:13px;color:#888;"> T1={n_t1} T2={n_t2} T3={n_t3} 共{total}信号</p>
</body></html>"""
    return html


def build_review_html(signals, date_str, notes=""):
    total = len(signals[1]) + len(signals[2]) + len(signals[3])
    n_t1, n_t2, n_t3 = len(signals[1]), len(signals[2]), len(signals[3])

    # 读取战绩
    track_stats = _get_track_stats()

    html = f"""<html><body style="font-family:Arial,sans-serif;color:#333;max-width:480px;">
<h2 style="color:#2c3e50;margin-bottom:4px;"> V10收盘日报</h2>
<p style="color:#888;font-size:13px;margin-top:0;">{date_str} 收盘后</p>

<p style="font-size:14px;"> 今日信号: T1=<b>{n_t1}</b> T2=<b>{n_t2}</b> T3=<b>{n_t3}</b> 共{total}</p>

{f'<p style="font-size:14px;"> {notes}</p>' if notes else ''}

{track_stats}

<p style="font-size:13px;color:#888;">策略健康度: 持续监控中</p>
</body></html>"""
    return html


def _get_track_stats():
    """从v10_track_record.csv读取战绩统计"""
    track_file = os.path.join(os.path.dirname(DEFAULT_CSV), 'v10_track_record.csv')
    if not os.path.exists(track_file):
        return ""

    records = []
    with open(track_file, encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            records.append(row)

    holding = [r for r in records if r.get('status') == 'holding']
    closed = [r for r in records if r.get('status') == 'closed']

    if not holding and not closed:
        return ""

    parts = ['<h3 style="color:#8e44ad;font-size:15px;"> 模拟组合战绩</h3>']

    if holding:
        parts.append(f'<p style="font-size:14px;"> 当前持仓: <b>{len(holding)}</b> 只</p>')
        for r in holding[:5]:
            parts.append(f'<p style="font-size:13px;margin:2px 0;">&nbsp;&nbsp;{r["code"]} {r["name"]} T{r["tier"]} ¥{r["entry_price"]}</p>')

    if closed:
        wins = [r for r in closed if float(r.get('pnl', 0)) > 0]
        wr = len(wins) / len(closed) * 100 if closed else 0
        total_pnl = sum(float(r.get('pnl', 0)) for r in closed)
        parts.append(f'<p style="font-size:14px;"> 已完成: {len(closed)}笔 | 胜率 <b>{wr:.0f}%</b> | 总盈亏 <b style="color:{"#e74c3c" if total_pnl < 0 else "#27ae60"}">¥{total_pnl:+,.0f}</b></p>')

    return '\n'.join(parts)


def send_email(subject, html_body, text_body=""):
    msg = MIMEMultipart("alternative")
    msg["From"] = SENDER
    msg["To"] = RECEIVER
    msg["Subject"] = subject

    if text_body:
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    server = smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT)
    server.login(SENDER, AUTH_CODE)
    server.sendmail(SENDER, RECEIVER, msg.as_string())
    server.quit()
    print(f"Email sent: {subject}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", required=True, choices=["prewarm", "decision", "review"])
    parser.add_argument("--csv", default=DEFAULT_CSV)
    parser.add_argument("--notes", default="")
    args = parser.parse_args()

    signals = load_signals(args.csv)
    date_str = datetime.now().strftime("%Y-%m-%d")

    if args.type == "prewarm":
        html = build_prewarm_html(signals, date_str)
        subject = f" V10预热 {date_str}"
        text = f"V10预热: T1={len(signals[1])} T2={len(signals[2])} T3={len(signals[3])}"
    elif args.type == "decision":
        html = build_decision_html(signals, date_str)
        subject = f" V10决策 {date_str}"
        text = f"V10决策: 买入清单已出"
    else:
        html = build_review_html(signals, date_str, args.notes)
        subject = f" V10日报 {date_str}"
        text = f"V10收盘日报"

    send_email(subject, html, text)


if __name__ == "__main__":
    main()
