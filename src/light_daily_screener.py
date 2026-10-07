from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pywencai
from kb_memory_bridge import (
    compact_memory_snapshot,
    load_market_kb_memory,
    render_market_kb_section,
    write_local_memory_snapshot,
)


ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "daily_reports"
LATEST_MD = ROOT / "latest_wencai_screen.md"
LATEST_JSON = ROOT / "latest_wencai_screen.json"

FORMULAS = {
    "趋势预备池": "沪深主板，非ST，非*ST，收盘价大于13日均线，13日均线大于34日均线，13日均线除以34日均线小于等于1.06，收盘价除以13日均线小于等于1.06，收盘价除以34日均线小于等于1.11，5日区间涨跌幅大于等于0%，5日区间涨跌幅小于等于14%，当日成交量除以20日均量大于等于0.7，当日成交量除以20日均量小于等于2.0，换手率大于等于1.2%，换手率小于等于8%，20日平均成交额大于等于8000万，总市值大于等于33亿元",
    "B1回踩池": "沪深主板，非ST，非*ST，收盘价大于34日均线，13日均线大于34日均线，13日均线除以34日均线小于等于1.05，最低价除以34日均线大于等于0.97，最低价除以34日均线小于等于1.02，收盘价除以34日均线小于等于1.05，收盘价除以13日均线小于等于1.03，收盘价大于等于开盘价，当日成交量除以5日均量小于等于1.05，当日涨幅小于等于1.5%，5日区间涨跌幅小于等于8%，换手率大于等于1.2%，换手率小于等于7%，20日平均成交额大于等于1亿，总市值大于等于33亿元",
    "B2回踩池": "沪深主板，非ST，非*ST，收盘价大于13日均线，13日均线大于34日均线，13日均线除以34日均线大于等于1.005，13日均线除以34日均线小于等于1.055，最低价除以13日均线大于等于0.97，最低价除以13日均线小于等于1.01，收盘价除以13日均线大于等于1，收盘价除以13日均线小于等于1.03，收盘价除以34日均线小于等于1.08，收盘价大于等于开盘价，当日成交量除以5日均量小于等于1.08，当日涨幅小于等于3%，5日区间涨跌幅大于等于1%，5日区间涨跌幅小于等于10%，换手率大于等于1.5%，换手率小于等于7%，20日平均成交额大于等于1亿，总市值大于等于33亿元",
    "起爆确认池": "沪深主板，非ST，非*ST，收盘价大于13日均线，13日均线大于34日均线，收盘价大于昨日最高价，收盘价除以13日均线小于等于1.05，收盘价除以最高价大于等于0.985，收盘价大于等于开盘价，当日涨幅大于等于1.8%，当日涨幅小于等于5.8%，当日成交量除以5日均量大于等于1.05，当日成交量除以5日均量小于等于2.3，5日区间涨跌幅小于等于12%，换手率大于等于1.5%，换手率小于等于8%，20日平均成交额大于等于1亿，总市值大于等于33亿元",
}


def to_frame(raw) -> pd.DataFrame:
    if raw is None:
        return pd.DataFrame()
    if isinstance(raw, pd.DataFrame):
        return raw.copy()
    if isinstance(raw, dict):
        return pd.DataFrame([raw])
    if isinstance(raw, list):
        return pd.DataFrame(raw)
    return pd.DataFrame(raw)


def find_col(df: pd.DataFrame, keywords: list[str]) -> str | None:
    for col in df.columns:
        col_text = str(col)
        if all(key in col_text for key in keywords):
            return col
    return None


def first_existing(df: pd.DataFrame, candidates: list[list[str]]) -> str | None:
    for keywords in candidates:
        col = find_col(df, keywords)
        if col:
            return col
    return None


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["code", "name", "price", "pct", "turnover", "amount", "market_cap", "industry"])

    code_col = first_existing(df, [["股票代码"], ["code"], ["证券代码"]])
    name_col = first_existing(df, [["股票简称"], ["股票名称"], ["名称"]])
    price_col = first_existing(df, [["最新价"], ["现价"], ["收盘价"]])
    pct_col = first_existing(df, [["最新涨跌幅"], ["涨跌幅"]])
    turn_col = first_existing(df, [["换手率"]])
    amount_col = first_existing(df, [["成交额"]])
    market_cap_col = first_existing(df, [["总市值"]])
    industry_col = first_existing(df, [["所属同花顺行业"], ["所属行业"]])

    out = pd.DataFrame()
    out["code"] = df[code_col].astype(str).str.extract(r"(\d{6})", expand=False) if code_col else ""
    out["name"] = df[name_col].astype(str) if name_col else ""
    out["price"] = pd.to_numeric(df[price_col], errors="coerce") if price_col else None
    out["pct"] = pd.to_numeric(df[pct_col], errors="coerce") if pct_col else None
    out["turnover"] = pd.to_numeric(df[turn_col], errors="coerce") if turn_col else None
    out["amount"] = pd.to_numeric(df[amount_col], errors="coerce") if amount_col else None
    out["market_cap"] = pd.to_numeric(df[market_cap_col], errors="coerce") if market_cap_col else None
    out["industry"] = df[industry_col].astype(str) if industry_col else ""
    out = out.dropna(subset=["code"]).drop_duplicates(subset=["code"]).reset_index(drop=True)
    return out


def score_rows(pool_name: str, df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if df.empty:
        df["score"] = []
        return df

    if pool_name == "起爆确认池":
        df["score"] = (
            df["pct"].fillna(0) * 1.3
            + df["turnover"].fillna(0) * 0.4
            + df["amount"].fillna(0) / 1e8 * 0.15
        )
        return df.sort_values(["score", "market_cap"], ascending=[False, False])

    if pool_name == "B2回踩池":
        df["score"] = (
            -df["pct"].fillna(0).abs() * 0.2
            + df["turnover"].fillna(0) * 0.25
            + df["amount"].fillna(0) / 1e8 * 0.12
        )
        return df.sort_values(["score", "market_cap"], ascending=[False, False])

    if pool_name == "B1回踩池":
        df["score"] = (
            -df["pct"].fillna(0).abs() * 0.15
            + df["turnover"].fillna(0) * 0.2
            + df["amount"].fillna(0) / 1e8 * 0.1
        )
        return df.sort_values(["score", "market_cap"], ascending=[False, False])

    df["score"] = (
        df["turnover"].fillna(0) * 0.25
        + df["amount"].fillna(0) / 1e8 * 0.1
        + df["pct"].fillna(0) * 0.1
    )
    return df.sort_values(["score", "market_cap"], ascending=[False, False])


def run_query(query: str) -> pd.DataFrame:
    raw = pywencai.get(query=query, loop=True)
    return normalize(to_frame(raw))


def build_markdown(
    run_time: str,
    pools: dict[str, pd.DataFrame],
    query_errors: dict[str, str] | None = None,
    market_kb_memory: dict | None = None,
    market_kb_export_path: Path | None = None,
    market_kb_snapshot_path: Path | None = None,
) -> str:
    lines = [
        "# 问财每日筛选",
        "",
        f"- 生成时间：{run_time}",
        "- 数据源：`pywencai`",
        "- 用途：收盘成交量四池筛选，次日再用30分钟结构确认",
        "",
        "## 总结",
        "",
        f"- 起爆确认池：`{len(pools['起爆确认池'])}`",
        f"- B2回踩池：`{len(pools['B2回踩池'])}`",
        f"- B1回踩池：`{len(pools['B1回踩池'])}`",
        f"- 趋势预备池：`{len(pools['趋势预备池'])}`",
        "",
    ]

    if query_errors:
        lines.extend(["## 查询异常", ""])
        for pool_name, error in query_errors.items():
            lines.append(f"- {pool_name}：`{error}`")
        lines.append("")

    if market_kb_snapshot_path:
        market_kb_section = render_market_kb_section(
            memory=market_kb_memory,
            export_path=market_kb_export_path,
            snapshot_path=market_kb_snapshot_path,
        ).strip()
        if market_kb_section:
            lines.extend(market_kb_section.splitlines())
            lines.append("")

    for pool_name in ["起爆确认池", "B2回踩池", "B1回踩池", "趋势预备池"]:
        df = pools[pool_name]
        lines.extend([f"## {pool_name}", ""])
        if query_errors and pool_name in query_errors:
            lines.append("- 本次问财接口查询失败，未生成有效结果。")
            lines.append("")
            continue
        if df.empty:
            lines.append("- 今日没有筛出标的。")
            lines.append("")
            continue
        lines.append("| 代码 | 名称 | 价格 | 涨跌幅 | 换手率 | 成交额(亿) | 总市值(亿) | 行业 |")
        lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |")
        for _, row in df.head(15).iterrows():
            amount = "" if pd.isna(row["amount"]) else f"{row['amount'] / 1e8:.2f}"
            market_cap = "" if pd.isna(row["market_cap"]) else f"{row['market_cap'] / 1e8:.1f}"
            price = "" if pd.isna(row["price"]) else f"{row['price']:.2f}"
            pct = "" if pd.isna(row["pct"]) else f"{row['pct']:.2f}%"
            turnover = "" if pd.isna(row["turnover"]) else f"{row['turnover']:.2f}%"
            lines.append(
                f"| {row['code']} | {row['name']} | {price} | {pct} | {turnover} | {amount} | {market_cap} | {row['industry']} |"
            )
        lines.append("")

    return "\n".join(lines) + "\n"


def main() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    run_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    pools: dict[str, pd.DataFrame] = {}
    query_errors: dict[str, str] = {}
    for pool_name, query in FORMULAS.items():
        try:
            df = run_query(query)
            pools[pool_name] = score_rows(pool_name, df)
        except Exception as exc:
            query_errors[pool_name] = f"{type(exc).__name__}: {exc}"
            pools[pool_name] = score_rows(pool_name, pd.DataFrame())

    market_kb_memory, market_kb_export_path = load_market_kb_memory()
    market_kb_snapshot_path = write_local_memory_snapshot(
        memory=market_kb_memory,
        export_path=market_kb_export_path,
    )

    report_text = build_markdown(
        run_time,
        pools,
        query_errors=query_errors,
        market_kb_memory=market_kb_memory,
        market_kb_export_path=market_kb_export_path,
        market_kb_snapshot_path=market_kb_snapshot_path,
    )
    dated_report = REPORT_DIR / f"{datetime.now().strftime('%Y-%m-%d')}-问财筛选.md"
    dated_report.write_text(report_text, encoding="utf-8")
    LATEST_MD.write_text(report_text, encoding="utf-8")

    json_payload = {
        "run_time": run_time,
        "counts": {name: len(df) for name, df in pools.items()},
        "top_candidates": {
            name: df.head(20).to_dict(orient="records") for name, df in pools.items()
        },
        "queries": FORMULAS,
        "query_errors": query_errors,
        "market_kb_memory": compact_memory_snapshot(market_kb_memory, market_kb_export_path),
        "market_kb_snapshot_path": str(market_kb_snapshot_path),
        "report_path": str(dated_report),
    }
    LATEST_JSON.write_text(json.dumps(json_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(json_payload["counts"], ensure_ascii=False, indent=2))
    print(f"问财日报已写入 {dated_report}")


if __name__ == "__main__":
    main()
