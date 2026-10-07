from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path

from backtest_formulas import FORMULAS, backtest, write_report as write_daily_formula_report
from backtest_30m_execution import bars_by_code_date, build_events, evaluate_execution, write_report as write_30m_report
from kb_memory_bridge import (
    compact_memory_snapshot,
    load_market_kb_memory,
    render_market_kb_section,
    write_local_memory_snapshot,
)
from market_data_store import (
    load_daily_klines_from_db,
    load_min30_from_db,
    sync_daily_bars,
    sync_min30_for_codes,
    sync_universe,
)


ROOT = Path(__file__).resolve().parents[1]
DAILY_REPORT_DIR = ROOT / "daily_reports"
LATEST_REPORT_PATH = ROOT / "latest_daily_backtest.md"
SNAPSHOT_JSON_PATH = ROOT / "latest_daily_backtest.json"


def avg(values: list[float]) -> float:
    return sum(values) / len(values)


def pct_change(now: float, prev: float) -> float:
    if prev == 0:
        return 0.0
    return (now / prev - 1.0) * 100.0


def safe_div(a: float, b: float) -> float:
    if not b:
        return 0.0
    return a / b


def build_latest_candidates(universe: dict, klines: dict) -> tuple[str, dict[str, list[dict]]]:
    latest_date = ""
    pools: dict[str, list[dict]] = {name: [] for name in FORMULAS}

    for code, candles in klines.items():
        meta = universe.get(code)
        if not meta or len(candles) < 40:
            continue

        closes = [c.close for c in candles]
        opens = [c.open for c in candles]
        lows = [c.low for c in candles]
        highs = [c.high for c in candles]
        vols = [c.volume_hands for c in candles]
        amts = [c.amount for c in candles]
        i = len(candles) - 1
        latest_date = candles[i].date

        ma13 = avg(closes[i - 12 : i + 1])
        ma34 = avg(closes[i - 33 : i + 1])
        vma5 = avg(vols[i - 4 : i + 1])
        amt20 = avg(amts[i - 19 : i + 1])
        market_cap = meta.total_shares * closes[i] if meta.total_shares else 0.0
        turnover = vols[i] * 100.0 / meta.float_shares * 100.0 if meta.float_shares else 0.0

        point = {
            "code": code,
            "name": meta.name,
            "close": closes[i],
            "open": opens[i],
            "low": lows[i],
            "high": highs[i],
            "ma13": ma13,
            "ma34": ma34,
            "vol_ratio_5": safe_div(vols[i], vma5),
            "amt20": amt20,
            "day_change": pct_change(closes[i], closes[i - 1]),
            "past5": pct_change(closes[i], closes[i - 5]),
            "turnover": turnover,
            "market_cap": market_cap,
            "prev_high": highs[i - 1],
            "close_ma13": safe_div(closes[i], ma13),
            "close_ma34": safe_div(closes[i], ma34),
            "low_ma13": safe_div(lows[i], ma13),
            "low_ma34": safe_div(lows[i], ma34),
        }

        for pool_name, fn in FORMULAS.items():
            if fn(point):
                pools[pool_name].append(point)

    def sort_rows(pool_name: str, rows: list[dict]) -> list[dict]:
        if pool_name == "趋势预备池":
            return sorted(rows, key=lambda x: (abs(x["close_ma13"] - 1.0), abs(x["close_ma34"] - 1.0), x["vol_ratio_5"]))
        if pool_name == "B1回踩池":
            return sorted(rows, key=lambda x: (abs(x["low_ma34"] - 1.0), abs(x["close_ma34"] - 1.0), x["vol_ratio_5"]))
        if pool_name == "B2回踩池":
            return sorted(rows, key=lambda x: (abs(x["low_ma13"] - 1.0), abs(x["close_ma13"] - 1.0), x["vol_ratio_5"]))
        return sorted(rows, key=lambda x: (-x["day_change"], x["close_ma13"], x["vol_ratio_5"]))

    pools = {name: sort_rows(name, rows) for name, rows in pools.items()}
    return latest_date, pools


def best_execution_rule(execution_summary: dict, pool_name: str) -> tuple[str, dict] | None:
    rules = execution_summary.get(pool_name) or {}
    if not rules:
        return None
    return max(
        rules.items(),
        key=lambda item: (item[1]["avg_ret3"], item[1]["avg_ret5"], item[1]["win3"], item[1]["trades"]),
    )


def build_daily_markdown(
    run_time: str,
    latest_trade_date: str,
    formula_summary: dict,
    benchmark: dict,
    execution_summary: dict,
    latest_candidates: dict[str, list[dict]],
    sync_info: dict,
    market_kb_memory: dict | None = None,
    market_kb_export_path: Path | None = None,
    market_kb_snapshot_path: Path | None = None,
) -> str:
    lines = [
        "# 每日回测日报",
        "",
        f"- 生成时间：{run_time}",
        f"- 最新交易日：{latest_trade_date}",
        f"- 主板股票池：`{sync_info['universe_count']}`",
        f"- 本地日线覆盖：`{sync_info['daily_kline_count']}`",
        f"- 日线同步新增：`{sync_info['daily_sync'].get('inserted_rows', 0)}` 行",
        f"- 30分钟同步新增：`{sync_info['min30_sync'].get('inserted_rows', 0)}` 行",
        "",
        "## 市场基准",
        "",
        f"- 全样本次日均值：`{benchmark['ret1']}%`",
        f"- 全样本3日均值：`{benchmark['ret3']}%`",
        f"- 全样本5日均值：`{benchmark['ret5']}%`",
        f"- 全样本3日胜率：`{benchmark['win3']}%`",
        "",
        "## 四池概览",
        "",
        "| 池子 | 平均每日出票 | 次日均值 | 3日均值 | 5日均值 | 3日胜率 | 最新日数量 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]

    for pool_name, row in formula_summary.items():
        latest_count = len(latest_candidates.get(pool_name, []))
        lines.append(
            f"| {pool_name} | {row['avg_count']} | {row['avg_ret1']}% | {row['avg_ret3']}% | {row['avg_ret5']}% | {row['avg_win3']}% | {latest_count} |"
        )

    lines.extend(
        [
            "",
            "## 今日结论",
            "",
            "- 趋势预备池、B1 回踩池、B2 回踩池继续当观察池，不要直接等同买点。",
            "- 起爆确认池仍然是最值得盯的执行层。",
            "- 如果最新日某个池子数量明显异常，优先检查数据同步覆盖，而不是第一时间改公式。",
            "",
        ]
    )

    if market_kb_snapshot_path:
        market_kb_section = render_market_kb_section(
            memory=market_kb_memory,
            export_path=market_kb_export_path,
            snapshot_path=market_kb_snapshot_path,
        ).strip()
        if market_kb_section:
            lines.extend(market_kb_section.splitlines())
            lines.append("")

    for pool_name in ["趋势预备池", "B1回踩池", "B2回踩池", "起爆确认池"]:
        rows = latest_candidates.get(pool_name, [])
        lines.extend([f"## {pool_name}", ""])
        lines.append(f"- 最新交易日数量：`{len(rows)}`")
        best_rule = best_execution_rule(execution_summary, pool_name)
        if best_rule:
            rule_name, stats = best_rule
            lines.append(
                f"- 当前回测里更像样的分钟规则：`{rule_name}`，3日均值 `{stats['avg_ret3']}%`，3日胜率 `{stats['win3']}%`"
            )
        else:
            lines.append("- 当前没有可用的分钟执行统计。")
        lines.append("")

        if not rows:
            lines.append("- 今日没有满足条件的标的。")
            lines.append("")
            continue

        lines.append("| 代码 | 名称 | 收盘 | 当日涨幅 | 5日涨幅 | 量比5 | 换手 | 20日均额(亿) | 总市值(亿) |")
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
        for row in rows[:12]:
            lines.append(
                f"| {row['code']} | {row['name']} | {row['close']:.2f} | {row['day_change']:.2f}% | {row['past5']:.2f}% | "
                f"{row['vol_ratio_5']:.2f} | {row['turnover']:.2f}% | {row['amt20'] / 1e8:.2f} | {row['market_cap'] / 1e8:.1f} |"
            )
        lines.append("")

    return "\n".join(lines) + "\n"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=60)
    parser.add_argument("--daily-start", default="2025-12-01")
    parser.add_argument("--full-refresh", action="store_true")
    parser.add_argument("--refresh-min30", action="store_true")
    args = parser.parse_args()

    DAILY_REPORT_DIR.mkdir(parents=True, exist_ok=True)

    universe = await sync_universe()
    daily_sync = sync_daily_bars(
        universe=universe,
        start_date=args.daily_start,
        full_refresh=args.full_refresh,
    )
    klines = load_daily_klines_from_db(universe, start_date=args.daily_start)

    formula_summary, benchmark = backtest(universe, klines, args.lookback_days)
    write_daily_formula_report(formula_summary, benchmark, args.lookback_days)

    latest_trade_date, latest_candidates = build_latest_candidates(universe, klines)

    events = build_events(universe, klines, args.lookback_days)
    codes = sorted({event.code for event in events})
    start_date = min(event.entry_date for event in events)
    end_date = max(event.entry_date for event in events)
    min30_sync = sync_min30_for_codes(codes, start_date, end_date, refresh=args.refresh_min30)
    min30 = load_min30_from_db(codes, start_date, end_date)
    min30_map = bars_by_code_date(min30)
    execution_summary = evaluate_execution(events, min30_map)
    write_30m_report(execution_summary, args.lookback_days)

    run_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sync_info = {
        "universe_count": len(universe),
        "daily_kline_count": len(klines),
        "daily_sync": daily_sync,
        "min30_sync": min30_sync,
    }
    market_kb_memory, market_kb_export_path = load_market_kb_memory()
    market_kb_snapshot_path = write_local_memory_snapshot(
        memory=market_kb_memory,
        export_path=market_kb_export_path,
    )
    report_text = build_daily_markdown(
        run_time=run_time,
        latest_trade_date=latest_trade_date,
        formula_summary=formula_summary,
        benchmark=benchmark,
        execution_summary=execution_summary,
        latest_candidates=latest_candidates,
        sync_info=sync_info,
        market_kb_memory=market_kb_memory,
        market_kb_export_path=market_kb_export_path,
        market_kb_snapshot_path=market_kb_snapshot_path,
    )

    dated_report = DAILY_REPORT_DIR / f"{datetime.now().strftime('%Y-%m-%d')}-回测日报.md"
    dated_report.write_text(report_text, encoding="utf-8")
    LATEST_REPORT_PATH.write_text(report_text, encoding="utf-8")

    snapshot = {
        "run_time": run_time,
        "latest_trade_date": latest_trade_date,
        "benchmark": benchmark,
        "formula_summary": formula_summary,
        "execution_summary": execution_summary,
        "latest_counts": {name: len(rows) for name, rows in latest_candidates.items()},
        "top_candidates": {name: rows[:12] for name, rows in latest_candidates.items()},
        "sync_info": sync_info,
        "market_kb_memory": compact_memory_snapshot(market_kb_memory, market_kb_export_path),
        "market_kb_snapshot_path": str(market_kb_snapshot_path),
        "report_path": str(dated_report),
    }
    SNAPSHOT_JSON_PATH.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({"latest_counts": snapshot["latest_counts"], "sync_info": sync_info}, ensure_ascii=False, indent=2))
    print(f"日报已写入: {dated_report}")


if __name__ == "__main__":
    asyncio.run(main())
