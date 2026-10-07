from __future__ import annotations

import argparse
import asyncio
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

import baostock as bs

from backtest_formulas import (
    CACHE_DIR,
    Candle,
    FORMULAS,
    UniverseRow,
    avg,
    fetch_klines,
    fetch_universe,
    pct_change,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIN30_PATH = CACHE_DIR / "min30_latest.json"
REPORT_PATH = PROJECT_ROOT / "output" / "30m_execution_backtest_report.md"


@dataclass
class MinBar:
    date: str
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float


@dataclass
class Event:
    pool: str
    code: str
    signal_date: str
    entry_date: str
    prev_close: float
    prev_high: float
    next_open: float
    fwd1_close: float
    fwd3_close: float
    fwd5_close: float


def safe_div(a: float, b: float) -> float:
    if not b:
        return 0.0
    return a / b


def market_code(code: str) -> str:
    return f"sz.{code}" if code.startswith(("000", "001", "002", "300")) else f"sh.{code}"


def build_events(universe: dict[str, UniverseRow], klines: dict[str, list[Candle]], lookback_days: int) -> list[Event]:
    events: list[Event] = []
    for code, candles in klines.items():
        meta = universe.get(code)
        if not meta or len(candles) < 42:
            continue

        closes = [c.close for c in candles]
        lows = [c.low for c in candles]
        highs = [c.high for c in candles]
        vols = [c.volume_hands for c in candles]
        amts = [c.amount for c in candles]

        start = max(34, len(candles) - lookback_days - 6)
        end = len(candles) - 6

        for i in range(start, end):
            ma13 = avg(closes[i - 12 : i + 1])
            ma34 = avg(closes[i - 33 : i + 1])
            vma5 = avg(vols[i - 4 : i + 1])
            amt20 = avg(amts[i - 19 : i + 1])
            market_cap = meta.total_shares * closes[i] if meta.total_shares else 0.0
            turnover = vols[i] * 100.0 / meta.float_shares * 100.0 if meta.float_shares else 0.0
            point = {
                "code": code,
                "date": candles[i].date,
                "close": closes[i],
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
            }

            for pool, fn in FORMULAS.items():
                if fn(point):
                    events.append(
                        Event(
                            pool=pool,
                            code=code,
                            signal_date=candles[i].date,
                            entry_date=candles[i + 1].date,
                            prev_close=closes[i],
                            prev_high=highs[i],
                            next_open=candles[i + 1].open,
                            fwd1_close=closes[i + 2],
                            fwd3_close=closes[i + 4],
                            fwd5_close=closes[i + 6],
                        )
                    )
    return events


def load_min30_cache() -> dict[str, list[MinBar]]:
    if not MIN30_PATH.exists():
        return {}
    raw = json.loads(MIN30_PATH.read_text(encoding="utf-8"))
    return {code: [MinBar(**row) for row in rows] for code, rows in raw.items()}


def save_min30_cache(data: dict[str, list[MinBar]]) -> None:
    MIN30_PATH.write_text(
        json.dumps({code: [asdict(row) for row in rows] for code, rows in data.items()}, ensure_ascii=False),
        encoding="utf-8",
    )


def fetch_min30_for_code(code: str, start_date: str, end_date: str) -> list[MinBar]:
    rs = bs.query_history_k_data_plus(
        market_code(code),
        "date,time,open,high,low,close,volume,amount",
        start_date=start_date,
        end_date=end_date,
        frequency="30",
        adjustflag="2",
    )
    out: list[MinBar] = []
    while rs.error_code == "0" and rs.next():
        d, t, o, h, l, c, v, amt = rs.get_row_data()
        out.append(
            MinBar(
                date=d,
                time=t[-9:-3],
                open=float(o),
                high=float(h),
                low=float(l),
                close=float(c),
                volume=float(v),
                amount=float(amt),
            )
        )
    return out


def fetch_min30(codes: list[str], start_date: str, end_date: str, refresh: bool = False) -> dict[str, list[MinBar]]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = {} if refresh else load_min30_cache()
    missing = [code for code in codes if code not in cache]
    if not missing:
        return cache

    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"baostock login failed: {login.error_msg}")
    try:
        for idx, code in enumerate(missing, start=1):
            cache[code] = fetch_min30_for_code(code, start_date, end_date)
            if idx % 20 == 0:
                save_min30_cache(cache)
    finally:
        bs.logout()

    save_min30_cache(cache)
    return cache


def bars_by_code_date(min30: dict[str, list[MinBar]]) -> dict[str, dict[str, list[MinBar]]]:
    out: dict[str, dict[str, list[MinBar]]] = defaultdict(lambda: defaultdict(list))
    for code, rows in min30.items():
        for row in rows:
            out[code][row.date].append(row)
    for code in out:
        for date in out[code]:
            out[code][date].sort(key=lambda x: x.time)
    return out


def trigger_pullback_prev1(bars: list[MinBar], prev_close: float) -> tuple[str, float] | None:
    day_open = bars[0].open
    for i in range(1, len(bars)):
        cur = bars[i]
        prev = bars[i - 1]
        if (
            cur.close > prev.high
            and cur.volume >= prev.volume * 1.05
            and cur.close >= day_open
            and cur.close <= prev_close * 1.03
        ):
            return cur.time, cur.close
    return None


def trigger_pullback_prev2(bars: list[MinBar], prev_close: float) -> tuple[str, float] | None:
    day_open = bars[0].open
    for i in range(2, len(bars)):
        cur = bars[i]
        prev_high = max(bars[i - 1].high, bars[i - 2].high)
        prev_vol = (bars[i - 1].volume + bars[i - 2].volume) / 2
        if (
            cur.close > prev_high
            and cur.volume >= prev_vol * 1.05
            and cur.close >= day_open
            and cur.close <= prev_close * 1.03
        ):
            return cur.time, cur.close
    return None


def trigger_pullback_first_high(bars: list[MinBar], prev_close: float) -> tuple[str, float] | None:
    first_high = bars[0].high
    day_open = bars[0].open
    for i in range(2, len(bars)):
        cur = bars[i]
        if cur.close > first_high and cur.close >= day_open and cur.close <= prev_close * 1.03:
            return cur.time, cur.close
    return None


def trigger_breakout_prev1(bars: list[MinBar], prev_close: float) -> tuple[str, float] | None:
    for i in range(1, len(bars)):
        cur = bars[i]
        prev = bars[i - 1]
        if (
            cur.close > prev.high
            and cur.volume >= prev.volume * 1.10
            and cur.close <= prev_close * 1.05
        ):
            return cur.time, cur.close
    return None


def trigger_breakout_prev2(bars: list[MinBar], prev_close: float) -> tuple[str, float] | None:
    for i in range(2, len(bars)):
        cur = bars[i]
        prev_high = max(bars[i - 1].high, bars[i - 2].high)
        prev_vol = (bars[i - 1].volume + bars[i - 2].volume) / 2
        if (
            cur.close > prev_high
            and cur.volume >= prev_vol * 1.10
            and cur.close <= prev_close * 1.05
        ):
            return cur.time, cur.close
    return None


def trigger_breakout_first_high(bars: list[MinBar], prev_close: float) -> tuple[str, float] | None:
    first_high = bars[0].high
    for i in range(2, len(bars)):
        cur = bars[i]
        prev_vol = (bars[i - 1].volume + bars[i - 2].volume) / 2
        if (
            cur.close > first_high
            and cur.volume >= prev_vol * 1.05
            and cur.close <= prev_close * 1.05
        ):
            return cur.time, cur.close
    return None


RULES = {
    "回踩执行A_前一根突破放量": trigger_pullback_prev1,
    "回踩执行B_两根平台突破": trigger_pullback_prev2,
    "回踩执行C_首根高点突破": trigger_pullback_first_high,
    "起爆执行A_前一根突破放量": trigger_breakout_prev1,
    "起爆执行B_两根平台突破": trigger_breakout_prev2,
    "起爆执行C_首根高点突破": trigger_breakout_first_high,
}


def rule_family(pool: str) -> list[str]:
    if pool == "起爆确认池":
        return [name for name in RULES if name.startswith("起爆")]
    return [name for name in RULES if name.startswith("回踩")]


def evaluate_execution(events: list[Event], min30_map: dict[str, dict[str, list[MinBar]]]) -> dict[str, dict[str, dict]]:
    result: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))

    for event in events:
        bars = min30_map.get(event.code, {}).get(event.entry_date, [])
        if len(bars) < 3:
            continue
        for rule_name in rule_family(event.pool):
            hit = RULES[rule_name](bars, event.prev_close)
            if not hit:
                continue
            time_str, entry_price = hit
            result[event.pool][rule_name].append(
                {
                    "time": time_str,
                    "entry_price": entry_price,
                    "ret1": pct_change(event.fwd1_close, entry_price),
                    "ret3": pct_change(event.fwd3_close, entry_price),
                    "ret5": pct_change(event.fwd5_close, entry_price),
                }
            )

    summary: dict[str, dict[str, dict]] = defaultdict(dict)
    for pool, rules in result.items():
        for rule_name, rows in rules.items():
            ret1 = [r["ret1"] for r in rows]
            ret3 = [r["ret3"] for r in rows]
            ret5 = [r["ret5"] for r in rows]
            summary[pool][rule_name] = {
                "trades": len(rows),
                "avg_ret1": round(avg(ret1), 3) if ret1 else 0.0,
                "avg_ret3": round(avg(ret3), 3) if ret3 else 0.0,
                "avg_ret5": round(avg(ret5), 3) if ret5 else 0.0,
                "win1": round(sum(x > 0 for x in ret1) / len(ret1) * 100, 1) if ret1 else 0.0,
                "win3": round(sum(x > 0 for x in ret3) / len(ret3) * 100, 1) if ret3 else 0.0,
                "win5": round(sum(x > 0 for x in ret5) / len(ret5) * 100, 1) if ret5 else 0.0,
            }
    return summary


def write_report(summary: dict[str, dict[str, dict]], lookback_days: int) -> None:
    lines = [
        "# 30分钟执行规则回测报告",
        "",
        f"回测窗口：最近 {lookback_days} 个交易日的日线入池信号，观察次日 30 分钟触发",
        "",
        "说明：",
        "- 先用日线公式选出当日信号。",
        "- 再看下一交易日的 30 分钟 K 线是否触发规则。",
        "- 入场价按触发那根 30 分钟K线的收盘价计算。",
        "- 后续收益观察入场后 1 日、3 日、5 日的日线收盘表现。",
        "",
    ]
    for pool, rules in summary.items():
        lines.extend(
            [
                f"## {pool}",
                "",
                "| 规则 | 触发笔数 | 1日均值 | 3日均值 | 5日均值 | 1日胜率 | 3日胜率 | 5日胜率 |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for rule_name, row in sorted(
            rules.items(),
            key=lambda item: (item[1]["avg_ret3"], item[1]["avg_ret5"], item[1]["trades"]),
            reverse=True,
        ):
            lines.append(
                f"| {rule_name} | {row['trades']} | {row['avg_ret1']}% | {row['avg_ret3']}% | "
                f"{row['avg_ret5']}% | {row['win1']}% | {row['win3']}% | {row['win5']}% |"
            )
        lines.append("")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=60)
    parser.add_argument("--refresh-daily", action="store_true")
    parser.add_argument("--refresh-min30", action="store_true")
    args = parser.parse_args()

    universe = await fetch_universe(refresh=args.refresh_daily)
    klines = await fetch_klines(universe, refresh=args.refresh_daily)
    events = build_events(universe, klines, args.lookback_days)

    codes = sorted({event.code for event in events})
    start_date = min(event.entry_date for event in events)
    end_date = max(event.entry_date for event in events)
    min30 = fetch_min30(codes, start_date, end_date, refresh=args.refresh_min30)
    min30_map = bars_by_code_date(min30)

    summary = evaluate_execution(events, min30_map)
    write_report(summary, args.lookback_days)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"报告已写入: {REPORT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
