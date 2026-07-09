import json
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from pytdx.hq import TdxHq_API


BASE_DIR = Path(__file__).resolve().parent
CONS_FILE = BASE_DIR / "000852cons.xls"
OFFICIAL_WEIGHT_FILE = BASE_DIR / "000852closeweight.xls"
VERIFY_OUT = BASE_DIR / "000852_weight_verification.csv"
SUMMARY_OUT = BASE_DIR / "000852_weight_summary.json"
REALTIME_OUT_TEMPLATE = "000852_realtime_weight_{date}.csv"
REALTIME_OUT_TIMESTAMP_TEMPLATE = "000852_realtime_weight_{timestamp}.csv"
REALTIME_OUT_JSONL_TIMESTAMP_TEMPLATE = "000852_realtime_weight_{timestamp}.jsonl"
SECTOR_FLOW_OUT_TIMESTAMP_TEMPLATE = "000852_sector_flow_{timestamp}.csv"
SECTOR_FLOW_OUT_JSONL_TIMESTAMP_TEMPLATE = "000852_sector_flow_{timestamp}.jsonl"
INDUSTRY_LOOKUP_OUT_TIMESTAMP_TEMPLATE = "000852_tdx_industry_lookup_{timestamp}.csv"
INDUSTRY_LOOKUP_OUT_JSONL_TIMESTAMP_TEMPLATE = "000852_tdx_industry_lookup_{timestamp}.jsonl"
REALTIME_MISSING_OUT = BASE_DIR / "000852_realtime_missing_quotes.csv"
TDXHY_CFG_FILE = BASE_DIR / "tdxhy.cfg"
INCON_DAT_FILE = BASE_DIR / "incon.dat"

TDX_HOSTS = [
    ("39.105.251.234", 7709),
    ("119.147.212.83", 7709),
    ("119.147.212.81", 7709),
    ("14.17.75.71", 7709),
    ("218.75.126.9", 7709),
    ("113.105.73.88", 7709),
    ("47.107.75.159", 7709),
]


def normalize_code(value: object) -> str:
    text = str(value).strip()
    if "." in text:
        text = text.split(".")[0]
    return text.zfill(6)


def market_from_exchange(exchange: str) -> int:
    return 0 if "深圳" in str(exchange) else 1


def parse_tdxhy_cfg(cfg_path: Path) -> dict[tuple[int, str], str]:
    if not cfg_path.exists():
        return {}
    mapping: dict[tuple[int, str], str] = {}
    with cfg_path.open("r", encoding="utf-8", errors="ignore") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) < 3:
                continue
            market_raw, code_raw, tdxhy_code_raw = parts[0], parts[1], parts[2]
            try:
                market = int(market_raw.strip())
            except Exception:
                continue
            code = normalize_code(code_raw)
            tdxhy_code = str(tdxhy_code_raw).strip()
            if not tdxhy_code:
                continue
            mapping[(market, code)] = tdxhy_code
    return mapping


def parse_incon_tdxnhy_name_map(incon_path: Path) -> dict[str, str]:
    if not incon_path.exists():
        return {}
    text = incon_path.read_text(encoding="gbk", errors="ignore")
    start = text.find("#TDXNHY")
    if start < 0:
        return {}
    end = text.find("######", start)
    chunk = text[start:end] if end > start else text[start:]
    result: dict[str, str] = {}
    for raw_line in chunk.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "|" not in line:
            continue
        code, name = line.split("|", 1)
        code = code.strip()
        name = name.strip()
        if not code or not name:
            continue
        result[code] = name
    return result


def connect_tdx(retry_rounds: int = 2) -> TdxHq_API:
    last_error = None
    for _ in range(retry_rounds):
        for host, port in TDX_HOSTS:
            api = TdxHq_API(heartbeat=True)
            try:
                if api.connect(host, port, time_out=1.5):
                    return api
            except Exception as exc:
                last_error = exc
            try:
                api.disconnect()
            except Exception:
                pass
        time.sleep(0.2)
    if last_error is None:
        raise RuntimeError("Unable to connect to any pytdx host.")
    raise RuntimeError(f"Unable to connect to any pytdx host: {last_error}")


def get_close_on_date(api: TdxHq_API, market: int, code: str, target_date: str) -> float:
    bars = api.get_security_bars(9, market, code, 0, 120)
    if not bars:
        raise RuntimeError(f"No daily bars for {code}")
    df = api.to_df(bars)
    if df is None or df.empty:
        raise RuntimeError(f"No daily dataframe for {code}")
    df["dt"] = pd.to_datetime(df["datetime"])
    df = df.sort_values("dt")
    df["date_str"] = df["dt"].dt.strftime("%Y%m%d")
    df = df[df["date_str"] <= target_date]
    if df.empty:
        raise RuntimeError(f"No bars on/before {target_date} for {code}")
    return float(df.iloc[-1]["close"])


def fetch_historical_closes(merged: pd.DataFrame, official_date: str) -> list[float]:
    closes: list[float] = []
    total_count = len(merged)
    api = connect_tdx(retry_rounds=3)
    try:
        for idx, row in merged.iterrows():
            market = int(row["market"])
            code = str(row["code"])
            attempts = 0
            while True:
                try:
                    close_price = get_close_on_date(api, market, code, official_date)
                    closes.append(close_price)
                    break
                except Exception:
                    attempts += 1
                    try:
                        api.disconnect()
                    except Exception:
                        pass
                    if attempts >= 5:
                        raise RuntimeError(f"Failed close data for {code} after {attempts} retries.")
                    time.sleep(0.15)
                    api = connect_tdx(retry_rounds=3)
            if (idx + 1) % 50 == 0 or idx + 1 == total_count:
                print(f"historical close progress: {idx + 1}/{total_count}")
    finally:
        try:
            api.disconnect()
        except Exception:
            pass
    return closes


def to_float_or_nan(value: object) -> float:
    try:
        if value is None:
            return np.nan
        text = str(value).strip()
        if text == "":
            return np.nan
        return float(text)
    except Exception:
        return np.nan


def get_realtime_prices(
    api: TdxHq_API, market_code_rows: list[tuple[int, str]]
) -> tuple[dict[tuple[int, str], dict[str, float]], dict[tuple[int, str], str]]:
    result: dict[tuple[int, str], dict[str, float]] = {}
    issues: dict[tuple[int, str], str] = {}
    chunk_size = 80
    for i in range(0, len(market_code_rows), chunk_size):
        chunk = market_code_rows[i : i + chunk_size]
        quotes = api.get_security_quotes(chunk)
        if not quotes:
            for pair in chunk:
                issues[pair] = "no_quotes_returned_for_chunk"
            continue
        returned_pairs: set[tuple[int, str]] = set()
        for q in quotes:
            market = int(q["market"])
            code = str(q["code"]).zfill(6)
            returned_pairs.add((market, code))
            price = to_float_or_nan(q.get("price"))
            last_close = to_float_or_nan(q.get("last_close"))
            transaction_value = to_float_or_nan(q.get("amount"))
            if price <= 0:
                issues[(market, code)] = f"non_positive_realtime_price(price={price},last_close={last_close})"
                continue
            result[(market, code)] = {
                "realtime_price": price,
                "last_close": last_close,
                "transaction_value": transaction_value,
            }
        chunk_set = set(chunk)
        for pair in chunk_set - returned_pairs:
            issues[pair] = "quote_not_returned_by_server"
    return result, issues


def fetch_realtime_prices_with_retry(
    market_code_rows: list[tuple[int, str]], rounds: int = 4
) -> tuple[dict[tuple[int, str], dict[str, float]], dict[tuple[int, str], str]]:
    result: dict[tuple[int, str], dict[str, float]] = {}
    issues: dict[tuple[int, str], str] = {}
    remaining = list(dict.fromkeys(market_code_rows))
    for _ in range(rounds):
        if not remaining:
            break
        api = connect_tdx(retry_rounds=3)
        try:
            round_result, round_issues = get_realtime_prices(api, remaining)
            result.update(round_result)
            issues.update(round_issues)
            remaining = [pair for pair in remaining if pair not in result]
            for pair in round_result:
                issues.pop(pair, None)
        finally:
            try:
                api.disconnect()
            except Exception:
                pass
        if remaining:
            time.sleep(0.2)
    for pair in remaining:
        issues.setdefault(pair, "unresolved_after_all_retries")
    return result, issues


def get_index_latest_level(index_market: int = 1, index_code: str = "000852") -> float:
    api = connect_tdx(retry_rounds=3)
    try:
        quotes = api.get_security_quotes([(index_market, index_code)])
        if not quotes:
            raise RuntimeError(f"No quote returned for index {index_code}")
        quote = quotes[0]
        latest = to_float_or_nan(quote.get("price"))
        if latest > 0:
            return latest
        last_close = to_float_or_nan(quote.get("last_close"))
        if last_close > 0:
            return last_close
        raise RuntimeError(f"Invalid index quote for {index_code}: {quote}")
    finally:
        try:
            api.disconnect()
        except Exception:
            pass


def fetch_share_capitals_with_retry(
    market_code_rows: list[tuple[int, str]], rounds: int = 4
) -> tuple[dict[tuple[int, str], dict[str, float]], dict[tuple[int, str], str]]:
    result: dict[tuple[int, str], dict[str, float]] = {}
    issues: dict[tuple[int, str], str] = {}
    remaining = list(dict.fromkeys(market_code_rows))
    for _ in range(rounds):
        if not remaining:
            break
        api = connect_tdx(retry_rounds=3)
        try:
            next_remaining: list[tuple[int, str]] = []
            for market, code in remaining:
                try:
                    info = api.get_finance_info(market, code)
                    if not info:
                        issues[(market, code)] = "empty_finance_info"
                        next_remaining.append((market, code))
                        continue
                    total_shares_10k = to_float_or_nan(info.get("zongguben"))
                    free_shares_10k = to_float_or_nan(info.get("liutongguben"))
                    if total_shares_10k <= 0 or free_shares_10k <= 0:
                        issues[(market, code)] = (
                            f"invalid_finance_shares(total={total_shares_10k},free={free_shares_10k})"
                        )
                        next_remaining.append((market, code))
                        continue
                    result[(market, code)] = {
                        "total_shares": total_shares_10k * 10000.0,
                        "free_shares": free_shares_10k * 10000.0,
                        "industry_code": int(info.get("industry", -1)),
                    }
                    issues.pop((market, code), None)
                except Exception as exc:
                    issues[(market, code)] = f"finance_info_error({type(exc).__name__})"
                    next_remaining.append((market, code))
            remaining = next_remaining
        finally:
            try:
                api.disconnect()
            except Exception:
                pass
        if remaining:
            time.sleep(0.2)
    for pair in remaining:
        issues.setdefault(pair, "unresolved_after_all_retries")
    return result, issues


def fetch_ytd_rise_with_retry(
    market_code_rows: list[tuple[int, str]], ytd_base_date: str, rounds: int = 4
) -> tuple[dict[tuple[int, str], dict[str, float]], dict[tuple[int, str], str]]:
    result: dict[tuple[int, str], dict[str, float]] = {}
    issues: dict[tuple[int, str], str] = {}
    remaining = list(dict.fromkeys(market_code_rows))
    for _ in range(rounds):
        if not remaining:
            break
        api = connect_tdx(retry_rounds=3)
        try:
            next_remaining: list[tuple[int, str]] = []
            for market, code in remaining:
                try:
                    ytd_base_close = get_close_on_date(api, market, code, ytd_base_date)
                    bars = api.get_security_bars(9, market, code, 0, 300)
                    if not bars:
                        issues[(market, code)] = "no_daily_bars"
                        next_remaining.append((market, code))
                        continue
                    df = api.to_df(bars)
                    if df is None or df.empty:
                        issues[(market, code)] = "empty_daily_dataframe"
                        next_remaining.append((market, code))
                        continue
                    df["dt"] = pd.to_datetime(df["datetime"])
                    df = df.sort_values("dt")
                    latest_close = to_float_or_nan(df.iloc[-1]["close"])
                    if ytd_base_close <= 0 or np.isnan(latest_close):
                        issues[(market, code)] = (
                            f"invalid_ytd_base_or_latest(base={ytd_base_close},latest={latest_close})"
                        )
                        next_remaining.append((market, code))
                        continue
                    result[(market, code)] = {
                        "ytd_base_close": ytd_base_close,
                        "ytd_rise_pct_to_latest_trade_day": (latest_close / ytd_base_close - 1.0) * 100.0,
                    }
                    issues.pop((market, code), None)
                except Exception as exc:
                    issues[(market, code)] = f"ytd_calc_error({type(exc).__name__})"
                    next_remaining.append((market, code))
            remaining = next_remaining
        finally:
            try:
                api.disconnect()
            except Exception:
                pass
        if remaining:
            time.sleep(0.2)
    for pair in remaining:
        issues.setdefault(pair, "unresolved_after_all_retries")
    return result, issues


def main() -> None:
    cons = pd.read_excel(CONS_FILE)
    official = pd.read_excel(OFFICIAL_WEIGHT_FILE)

    cons = cons.rename(
        columns={
            "成份券代码Constituent Code": "code_raw",
            "成份券名称Constituent Name": "name",
            "交易所Exchange": "exchange",
        }
    )
    official = official.rename(
        columns={
            "成份券代码Constituent Code": "code_raw",
            "成份券名称Constituent Name": "name",
            "交易所Exchange": "exchange",
            "权重(%)weight": "official_weight",
            "日期Date": "official_date",
        }
    )

    cons["code"] = cons["code_raw"].map(normalize_code)
    official["code"] = official["code_raw"].map(normalize_code)
    cons["market"] = cons["exchange"].map(market_from_exchange)
    official["market"] = official["exchange"].map(market_from_exchange)
    official["official_weight"] = official["official_weight"].astype(float)
    official_date = str(int(official["official_date"].iloc[0]))

    merged = official[["code", "name", "exchange", "market", "official_weight"]].copy()
    merged = merged.drop_duplicates(subset=["market", "code"]).reset_index(drop=True)

    merged["close_ref"] = fetch_historical_closes(merged, official_date)

    merged["unit"] = merged["official_weight"] / merged["close_ref"]
    merged["rebuilt_weight"] = merged["close_ref"] * merged["unit"]
    merged["diff"] = merged["rebuilt_weight"] - merged["official_weight"]

    market_code_pairs = [(int(r["market"]), str(r["code"])) for _, r in merged.iterrows()]
    realtime_map, realtime_issues = fetch_realtime_prices_with_retry(market_code_pairs, rounds=5)
    csi1000_latest_level = get_index_latest_level(index_market=1, index_code="000852")

    merged["realtime_price"] = merged.apply(
        lambda r: realtime_map.get((int(r["market"]), str(r["code"])), {}).get("realtime_price", np.nan), axis=1
    )
    merged["t1_close"] = merged.apply(
        lambda r: realtime_map.get((int(r["market"]), str(r["code"])), {}).get("last_close", np.nan), axis=1
    )
    merged["transaction_value"] = merged.apply(
        lambda r: realtime_map.get((int(r["market"]), str(r["code"])), {}).get("transaction_value", np.nan), axis=1
    )

    missing_rows = merged[merged["realtime_price"].isna()].copy()
    if not missing_rows.empty:
        missing_rows["reason"] = missing_rows.apply(
            lambda r: realtime_issues.get((int(r["market"]), str(r["code"])), "unknown_reason"), axis=1
        )
        missing_out = missing_rows[["code", "name", "exchange", "reason"]].copy()
        missing_out.to_csv(REALTIME_MISSING_OUT, index=False, encoding="utf-8-sig")
        reason_counts = missing_out["reason"].value_counts().to_dict()
        raise RuntimeError(
            f"Missing realtime prices for {len(missing_out)} securities. "
            f"Details saved to {REALTIME_MISSING_OUT}. Reason counts: {reason_counts}"
        )

    share_map, share_issues = fetch_share_capitals_with_retry(market_code_pairs, rounds=5)
    ytd_base_date = "20251231"
    ytd_detail_map, ytd_issues = fetch_ytd_rise_with_retry(market_code_pairs, ytd_base_date=ytd_base_date, rounds=5)

    merged["rise_pct_vs_t1_close"] = np.where(
        merged["t1_close"] > 0, (merged["realtime_price"] / merged["t1_close"] - 1.0) * 100.0, np.nan
    )
    merged["realtime_raw"] = merged["unit"] * merged["realtime_price"]
    merged["realtime_weight"] = merged["realtime_raw"] / merged["realtime_raw"].sum() * 100.0
    merged["contribute_points_csi1000"] = (
        merged["realtime_weight"] / 100.0 * merged["rise_pct_vs_t1_close"] / 100.0 * csi1000_latest_level
    )
    merged["market_cap_total"] = merged.apply(
        lambda r: realtime_map.get((int(r["market"]), str(r["code"])), {}).get("realtime_price", np.nan)
        * share_map.get((int(r["market"]), str(r["code"])), {}).get("total_shares", np.nan),
        axis=1,
    )
    merged["market_cap_free"] = merged.apply(
        lambda r: realtime_map.get((int(r["market"]), str(r["code"])), {}).get("realtime_price", np.nan)
        * share_map.get((int(r["market"]), str(r["code"])), {}).get("free_shares", np.nan),
        axis=1,
    )
    merged["ytd_base_20251231_close_price"] = merged.apply(
        lambda r: ytd_detail_map.get((int(r["market"]), str(r["code"])), {}).get("ytd_base_close", np.nan), axis=1
    )
    merged["ytd_rise_pct_to_latest_trade_day"] = merged.apply(
        lambda r: ytd_detail_map.get((int(r["market"]), str(r["code"])), {}).get(
            "ytd_rise_pct_to_latest_trade_day", np.nan
        ),
        axis=1,
    )
    merged = merged.sort_values("realtime_weight", ascending=False).reset_index(drop=True)

    verify_cols = ["code", "name", "exchange", "official_weight", "close_ref", "rebuilt_weight", "diff"]
    verify_df = merged[verify_cols].copy()
    verify_df.to_csv(VERIFY_OUT, index=False, encoding="utf-8-sig")

    now = datetime.now()
    today = now.strftime("%Y%m%d")
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    realtime_out = BASE_DIR / REALTIME_OUT_TEMPLATE.format(date=today)
    realtime_out_timestamp = BASE_DIR / REALTIME_OUT_TIMESTAMP_TEMPLATE.format(timestamp=timestamp)
    realtime_out_jsonl_timestamp = BASE_DIR / REALTIME_OUT_JSONL_TIMESTAMP_TEMPLATE.format(timestamp=timestamp)
    sector_flow_out_timestamp = BASE_DIR / SECTOR_FLOW_OUT_TIMESTAMP_TEMPLATE.format(timestamp=timestamp)
    sector_flow_out_jsonl_timestamp = BASE_DIR / SECTOR_FLOW_OUT_JSONL_TIMESTAMP_TEMPLATE.format(timestamp=timestamp)
    industry_lookup_out_timestamp = BASE_DIR / INDUSTRY_LOOKUP_OUT_TIMESTAMP_TEMPLATE.format(timestamp=timestamp)
    industry_lookup_out_jsonl_timestamp = BASE_DIR / INDUSTRY_LOOKUP_OUT_JSONL_TIMESTAMP_TEMPLATE.format(
        timestamp=timestamp
    )
    merged["tdx_industry_code"] = merged.apply(
        lambda r: share_map.get((int(r["market"]), str(r["code"])), {}).get("industry_code", np.nan), axis=1
    )
    tdxhy_code_map = parse_tdxhy_cfg(TDXHY_CFG_FILE)
    tdxhy_name_map = parse_incon_tdxnhy_name_map(INCON_DAT_FILE)
    merged["tdxhy_industry_code"] = merged.apply(
        lambda r: tdxhy_code_map.get((int(r["market"]), str(r["code"])), None), axis=1
    )
    merged["tdx_industry_name"] = merged["tdxhy_industry_code"].apply(
        lambda code: tdxhy_name_map.get(str(code), "UNKNOWN") if pd.notna(code) else "UNKNOWN"
    )
    merged["tdx_sector"] = merged.apply(
        lambda r: f"TDXHY_{r['tdxhy_industry_code']}"
        if pd.notna(r["tdxhy_industry_code"]) and str(r["tdxhy_industry_code"]).strip()
        else ("TDX_IND_UNKNOWN" if pd.isna(r["tdx_industry_code"]) or int(r["tdx_industry_code"]) < 0 else f"TDX_IND_{int(r['tdx_industry_code']):02d}"),
        axis=1,
    )
    merged["flow_pressure_score"] = merged["transaction_value"] * (merged["rise_pct_vs_t1_close"] / 100.0)
    realtime_cols = [
        "code",
        "name",
        "exchange",
        "tdx_sector",
        "tdx_industry_code",
        "tdx_industry_name",
        "tdxhy_industry_code",
        "realtime_price",
        "realtime_weight",
        "rise_pct_vs_t1_close",
        "transaction_value",
        "flow_pressure_score",
        "contribute_points_csi1000",
        "market_cap_total",
        "market_cap_free",
        "ytd_base_20251231_close_price",
        "ytd_rise_pct_to_latest_trade_day",
    ]
    realtime_df = merged[realtime_cols].copy()
    realtime_df.to_csv(realtime_out, index=False, encoding="utf-8-sig")
    realtime_df.to_csv(realtime_out_timestamp, index=False, encoding="utf-8-sig")
    realtime_records = []
    for row in realtime_df.to_dict(orient="records"):
        normalized_row = {}
        for key, value in row.items():
            if pd.isna(value):
                normalized_row[key] = None
            elif isinstance(value, np.generic):
                normalized_row[key] = value.item()
            else:
                normalized_row[key] = value
        realtime_records.append(normalized_row)
    with realtime_out_jsonl_timestamp.open("w", encoding="utf-8") as f:
        for record in realtime_records:
            f.write(json.dumps(record, ensure_ascii=False))
            f.write("\n")
    sector_flow_df = (
        merged.groupby(["tdx_sector", "tdxhy_industry_code", "tdx_industry_name"], dropna=False, as_index=False)
        .agg(
            stock_count=("code", "count"),
            total_transaction_value=("transaction_value", "sum"),
            avg_rise_pct_vs_t1_close=("rise_pct_vs_t1_close", "mean"),
            total_flow_pressure_score=("flow_pressure_score", "sum"),
            total_contribute_points_csi1000=("contribute_points_csi1000", "sum"),
        )
        .sort_values("total_flow_pressure_score", ascending=False)
        .reset_index(drop=True)
    )
    sector_flow_df["flow_direction"] = np.where(
        sector_flow_df["total_flow_pressure_score"] > 0,
        "upward_pressure",
        np.where(sector_flow_df["total_flow_pressure_score"] < 0, "downward_pressure", "neutral"),
    )
    sector_flow_df.to_csv(sector_flow_out_timestamp, index=False, encoding="utf-8-sig")
    with sector_flow_out_jsonl_timestamp.open("w", encoding="utf-8") as f:
        for record in sector_flow_df.to_dict(orient="records"):
            normalized_record = {}
            for key, value in record.items():
                if pd.isna(value):
                    normalized_record[key] = None
                elif isinstance(value, np.generic):
                    normalized_record[key] = value.item()
                else:
                    normalized_record[key] = value
            f.write(json.dumps(normalized_record, ensure_ascii=False))
            f.write("\n")
    industry_lookup_rows = []
    for tdxhy_code, group in merged.groupby("tdxhy_industry_code", dropna=False):
        top_names = group.sort_values("realtime_weight", ascending=False)["name"].astype(str).head(5).tolist()
        industry_name = str(group["tdx_industry_name"].iloc[0]) if not group.empty else "UNKNOWN"
        finance_codes = (
            group["tdx_industry_code"].dropna().astype(int).drop_duplicates().sort_values().astype(str).tolist()
        )
        industry_lookup_rows.append(
            {
                "tdx_industry_code": ",".join(finance_codes),
                "tdxhy_industry_code": None if pd.isna(tdxhy_code) else str(tdxhy_code),
                "tdx_sector": f"TDXHY_{tdxhy_code}" if pd.notna(tdxhy_code) else "TDX_IND_UNKNOWN",
                "tdx_industry_name": industry_name,
                "stock_count": int(len(group)),
                "representative_stocks_top5": "、".join(top_names),
                "total_transaction_value": float(group["transaction_value"].sum()),
                "total_flow_pressure_score": float(group["flow_pressure_score"].sum()),
            }
        )
    industry_lookup_df = (
        pd.DataFrame(industry_lookup_rows)
        .sort_values("total_flow_pressure_score", ascending=False)
        .reset_index(drop=True)
    )
    industry_lookup_df["flow_direction"] = np.where(
        industry_lookup_df["total_flow_pressure_score"] > 0,
        "upward_pressure",
        np.where(industry_lookup_df["total_flow_pressure_score"] < 0, "downward_pressure", "neutral"),
    )
    industry_lookup_df.to_csv(industry_lookup_out_timestamp, index=False, encoding="utf-8-sig")
    with industry_lookup_out_jsonl_timestamp.open("w", encoding="utf-8") as f:
        for record in industry_lookup_df.to_dict(orient="records"):
            normalized_record = {}
            for key, value in record.items():
                if pd.isna(value):
                    normalized_record[key] = None
                elif isinstance(value, np.generic):
                    normalized_record[key] = value.item()
                else:
                    normalized_record[key] = value
            f.write(json.dumps(normalized_record, ensure_ascii=False))
            f.write("\n")

    summary = {
        "official_date": official_date,
        "constituent_count": int(len(merged)),
        "official_weight_sum": float(merged["official_weight"].sum()),
        "rebuilt_weight_sum": float(merged["rebuilt_weight"].sum()),
        "max_abs_diff": float(merged["diff"].abs().max()),
        "mean_abs_diff": float(merged["diff"].abs().mean()),
        "exact_match_count_3dp": int((merged["rebuilt_weight"].round(3) == merged["official_weight"].round(3)).sum()),
        "realtime_weight_sum": float(merged["realtime_weight"].sum()),
        "csi1000_latest_level": float(csi1000_latest_level),
        "market_cap_total_missing_count": int(merged["market_cap_total"].isna().sum()),
        "market_cap_free_missing_count": int(merged["market_cap_free"].isna().sum()),
        "ytd_base_close_missing_count": int(merged["ytd_base_20251231_close_price"].isna().sum()),
        "ytd_rise_missing_count": int(merged["ytd_rise_pct_to_latest_trade_day"].isna().sum()),
        "share_issue_count": int(len(share_issues)),
        "ytd_issue_count": int(len(ytd_issues)),
        "tdxhy_cfg_exists": bool(TDXHY_CFG_FILE.exists()),
        "incon_dat_exists": bool(INCON_DAT_FILE.exists()),
        "tdxhy_mapped_stock_count": int(merged["tdxhy_industry_code"].notna().sum()),
        "tdxhy_name_mapped_stock_count": int((merged["tdx_industry_name"] != "UNKNOWN").sum()),
        "ytd_base_date": ytd_base_date,
        "verify_file": str(VERIFY_OUT),
        "realtime_file": str(realtime_out),
        "realtime_file_timestamp": str(realtime_out_timestamp),
        "realtime_file_jsonl_timestamp": str(realtime_out_jsonl_timestamp),
        "sector_flow_file_timestamp": str(sector_flow_out_timestamp),
        "sector_flow_file_jsonl_timestamp": str(sector_flow_out_jsonl_timestamp),
        "industry_lookup_file_timestamp": str(industry_lookup_out_timestamp),
        "industry_lookup_file_jsonl_timestamp": str(industry_lookup_out_jsonl_timestamp),
    }
    SUMMARY_OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
