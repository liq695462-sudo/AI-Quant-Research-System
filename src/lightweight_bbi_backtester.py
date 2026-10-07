from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import aiohttp
import efinance as ef


ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "cache" / "lightweight_bbi_backtest"
OUT_DIR = ROOT / "output" / "lightweight_bbi_backtest"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://quote.eastmoney.com/",
}

MAINBOARD_PREFIXES = ("000", "001", "002", "600", "601", "603", "605")
BLOCKED_NAME_PARTS = ("ST", "*ST", "退")


@dataclass
class UniverseRow:
    code: str
    name: str
    price: float
    market_cap: float
    float_cap: float
    latest_trade_date: str

    @property
    def total_shares(self) -> float:
        return self.market_cap / self.price if self.market_cap > 0 and self.price > 0 else 0.0


@dataclass
class Candle:
    date: str
    open: float
    close: float
    high: float
    low: float
    volume_hands: float
    amount: float
    turnover: float
    pct_chg: float


def safe_float(value) -> float:
    try:
        if value in ("-", "", None):
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def med(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


def pct_change(now: float, prev: float) -> float:
    return (now / prev - 1.0) * 100.0 if prev else 0.0


def cache_paths(cache_key: str) -> tuple[Path, Path]:
    return CACHE_DIR / f"universe_{cache_key}.json", CACHE_DIR / f"klines_{cache_key}.json"


def load_universe(refresh: bool, cache_key: str) -> dict[str, UniverseRow]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    universe_path, _ = cache_paths(cache_key)
    if universe_path.exists() and not refresh:
        raw = json.loads(universe_path.read_text(encoding="utf-8"))
        return {code: UniverseRow(**row) for code, row in raw.items()}

    df = ef.stock.get_realtime_quotes()
    out: dict[str, UniverseRow] = {}
    for _, row in df.iterrows():
        code = str(row.get("股票代码", "")).strip()
        name = str(row.get("股票名称", "")).strip()
        if not code.startswith(MAINBOARD_PREFIXES):
            continue
        if any(part in name for part in BLOCKED_NAME_PARTS):
            continue
        price = safe_float(row.get("最新价"))
        market_cap = safe_float(row.get("总市值"))
        float_cap = safe_float(row.get("流通市值"))
        latest_trade_date = str(row.get("最新交易日", "")).strip()
        if price <= 0 or market_cap <= 0:
            continue
        out[code] = UniverseRow(code, name, price, market_cap, float_cap, latest_trade_date)

    universe_path.write_text(
        json.dumps({code: asdict(row) for code, row in out.items()}, ensure_ascii=False),
        encoding="utf-8",
    )
    return out


async def fetch_json(session: aiohttp.ClientSession, url: str, params: dict) -> dict | None:
    for attempt in range(3):
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=18)) as resp:
                return await resp.json(content_type=None)
        except Exception:
            if attempt == 2:
                return None
            await asyncio.sleep(0.25)
    return None


async def fetch_one_kline(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    code: str,
    beg: str,
    end: str,
    limit: int,
) -> tuple[str, list[Candle]]:
    market = "0" if code.startswith(("000", "001", "002")) else "1"
    params = {
        "secid": f"{market}.{code}",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": "1",
        "beg": beg,
        "end": end,
        "lmt": str(limit),
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
    }
    async with sem:
        payload = await fetch_json(session, "https://push2his.eastmoney.com/api/qt/stock/kline/get", params)
    rows = (((payload or {}).get("data") or {}).get("klines")) or []
    candles: list[Candle] = []
    for line in rows:
        parts = line.split(",")
        if len(parts) < 11:
            continue
        d, o, c, h, l, v, amt, _amp, pct, _chg, turn = parts[:11]
        candles.append(
            Candle(
                date=d,
                open=safe_float(o),
                close=safe_float(c),
                high=safe_float(h),
                low=safe_float(l),
                volume_hands=safe_float(v),
                amount=safe_float(amt),
                turnover=safe_float(turn),
                pct_chg=safe_float(pct),
            )
        )
    return code, candles


async def load_klines(
    universe: dict[str, UniverseRow],
    refresh: bool,
    cache_key: str,
    beg: str,
    end: str,
    limit: int,
    concurrency: int,
) -> dict[str, list[Candle]]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _, kline_path = cache_paths(cache_key)
    if kline_path.exists() and not refresh:
        raw = json.loads(kline_path.read_text(encoding="utf-8"))
        return {code: [Candle(**row) for row in rows] for code, rows in raw.items()}

    connector = aiohttp.TCPConnector(limit=concurrency, ssl=False)
    sem = asyncio.Semaphore(concurrency)
    async with aiohttp.ClientSession(headers=HEADERS, trust_env=False, connector=connector) as session:
        tasks = [fetch_one_kline(session, sem, code, beg, end, limit) for code in universe]
        results = await asyncio.gather(*tasks)

    out = {code: rows for code, rows in results if len(rows) >= 70}
    kline_path.write_text(
        json.dumps({code: [asdict(c) for c in rows] for code, rows in out.items()}, ensure_ascii=False),
        encoding="utf-8",
    )
    return out


def make_point(code: str, name: str, candles: list[Candle], i: int, total_shares: float) -> dict | None:
    if i < 64:
        return None
    closes = [c.close for c in candles]
    opens = [c.open for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    vols = [c.volume_hands for c in candles]
    amts = [c.amount for c in candles]

    ma3 = avg(closes[i - 2 : i + 1])
    ma6 = avg(closes[i - 5 : i + 1])
    ma12 = avg(closes[i - 11 : i + 1])
    ma24 = avg(closes[i - 23 : i + 1])
    bbi = (ma3 + ma6 + ma12 + ma24) / 4.0
    j = i - 3
    bbi_3 = (avg(closes[j - 2 : j + 1]) + avg(closes[j - 5 : j + 1]) + avg(closes[j - 11 : j + 1]) + avg(closes[j - 23 : j + 1])) / 4.0
    ma60 = avg(closes[i - 59 : i + 1])
    ma60_5 = avg(closes[i - 64 : i - 4])
    vma5 = avg(vols[i - 4 : i + 1])
    vma20 = avg(vols[i - 19 : i + 1])
    amt20 = avg(amts[i - 19 : i + 1])
    market_cap = total_shares * closes[i] if total_shares else 0.0

    return {
        "date": candles[i].date,
        "code": code,
        "name": name,
        "open": opens[i],
        "close": closes[i],
        "high": highs[i],
        "low": lows[i],
        "bbi": bbi,
        "bbi_3": bbi_3,
        "ma60": ma60,
        "ma60_5": ma60_5,
        "vol_ratio_5": safe_div(vols[i], vma5),
        "vol_ratio_20": safe_div(vols[i], vma20),
        "turnover": candles[i].turnover,
        "amt20": amt20,
        "amount": amts[i],
        "market_cap": market_cap,
        "day_change": pct_change(closes[i], closes[i - 1]),
        "past3": pct_change(closes[i], closes[i - 3]),
        "past5": pct_change(closes[i], closes[i - 5]),
        "past10": pct_change(closes[i], closes[i - 10]),
        "prev_high": highs[i - 1],
        "prev3_high": max(highs[i - 3 : i]),
        "prev5_high": max(highs[i - 5 : i]),
        "close_bbi": safe_div(closes[i], bbi),
        "close_ma60": safe_div(closes[i], ma60),
        "low_bbi": safe_div(lows[i], bbi),
        "low_ma60": safe_div(lows[i], ma60),
        "close_high": safe_div(closes[i], highs[i]),
        "upper_shadow": safe_div(highs[i] - max(opens[i], closes[i]), closes[i]),
        "fwd1": pct_change(closes[i + 1], closes[i]) if i + 1 < len(candles) else None,
        "fwd3": pct_change(closes[i + 3], closes[i]) if i + 3 < len(candles) else None,
        "fwd5": pct_change(closes[i + 5], closes[i]) if i + 5 < len(candles) else None,
    }


def build_points(universe: dict[str, UniverseRow], klines: dict[str, list[Candle]], lookback_days: int) -> tuple[dict[str, list[dict]], dict[str, list[dict]], str, str]:
    backtest_by_date: dict[str, list[dict]] = {}
    current_by_date: dict[str, list[dict]] = {}

    for code, candles in klines.items():
        meta = universe.get(code)
        if not meta or len(candles) < 70:
            continue
        bt_start = max(64, len(candles) - lookback_days - 5)
        bt_end = len(candles) - 5
        for i in range(bt_start, bt_end):
            point = make_point(code, meta.name, candles, i, meta.total_shares)
            if point and point["fwd5"] is not None:
                backtest_by_date.setdefault(point["date"], []).append(point)

        point = make_point(code, meta.name, candles, len(candles) - 1, meta.total_shares)
        if point:
            current_by_date.setdefault(point["date"], []).append(point)

    backtest_signal_end = max(backtest_by_date) if backtest_by_date else ""
    current_latest = max(current_by_date) if current_by_date else ""
    return backtest_by_date, current_by_date, backtest_signal_end, current_latest


def tradeable(p: dict, amt20: float, cap: float, turn_low: float, turn_high: float) -> bool:
    return p["amt20"] >= amt20 and p["market_cap"] >= cap and turn_low <= p["turnover"] <= turn_high and p["close"] >= 3.0


def old_trend(p: dict) -> bool:
    return tradeable(p, 1e8, 4e9, 1.2, 7.5) and p["close"] > p["bbi"] > p["ma60"] and p["bbi"] >= p["bbi_3"] and p["close_bbi"] <= 1.04 and p["close_ma60"] <= 1.12 and 0 <= p["past5"] <= 11 and 0.65 <= p["vol_ratio_20"] <= 1.65


def old_b1(p: dict) -> bool:
    return tradeable(p, 1.2e8, 4e9, 1.3, 7.5) and p["close"] > p["ma60"] and p["bbi"] > p["ma60"] and p["ma60"] >= p["ma60_5"] and 0.985 <= p["low_ma60"] <= 1.025 and p["close_ma60"] <= 1.04 and p["close_bbi"] <= 1.035 and p["close"] >= p["open"] and p["vol_ratio_5"] <= 1.0 and p["day_change"] <= 1.5 and p["past5"] <= 8


def old_b2(p: dict) -> bool:
    return tradeable(p, 1.2e8, 4e9, 1.5, 7.5) and p["close"] > p["bbi"] > p["ma60"] and p["bbi"] >= p["bbi_3"] and 0.985 <= p["low_bbi"] <= 1.015 and 1.0 <= p["close_bbi"] <= 1.025 and p["close_ma60"] <= 1.09 and p["close"] >= p["open"] and p["vol_ratio_5"] <= 1.05 and p["day_change"] <= 2.8 and 0 <= p["past5"] <= 9


def old_breakout(p: dict) -> bool:
    return tradeable(p, 1.2e8, 4e9, 2.0, 7.5) and p["close"] > p["bbi"] > p["ma60"] and p["bbi"] >= p["bbi_3"] and p["close"] > p["prev_high"] and p["close_bbi"] <= 1.045 and p["close_high"] >= 0.99 and p["close"] >= p["open"] and 2.2 <= p["day_change"] <= 5.3 and 1.15 <= p["vol_ratio_5"] <= 2.0 and p["past5"] <= 10


def final_trend(p: dict) -> bool:
    return tradeable(p, 1.5e8, 5e9, 1.5, 6.5) and p["close"] > p["bbi"] > p["ma60"] and p["bbi"] > p["bbi_3"] and 1.0 <= p["close_bbi"] <= 1.032 and p["close_ma60"] <= 1.085 and 0 <= p["past5"] <= 7.5 and p["past3"] <= 5.5 and 0.70 <= p["vol_ratio_20"] <= 1.45


def final_b1(p: dict) -> bool:
    return tradeable(p, 1.5e8, 5e9, 1.4, 6.8) and p["close"] > p["ma60"] and p["bbi"] > p["ma60"] and p["ma60"] > p["ma60_5"] and 0.990 <= p["low_ma60"] <= 1.018 and 1.000 <= p["close_ma60"] <= 1.028 and p["close_bbi"] <= 1.022 and -1.8 <= p["day_change"] <= 1.2 and p["vol_ratio_5"] <= 0.92 and p["past5"] <= 6.5 and p["past10"] >= -4


def final_b2(p: dict) -> bool:
    return tradeable(p, 1.5e8, 5e9, 1.6, 6.8) and p["close"] > p["bbi"] > p["ma60"] and p["bbi"] > p["bbi_3"] and 0.992 <= p["low_bbi"] <= 1.010 and 1.000 <= p["close_bbi"] <= 1.018 and p["close_ma60"] <= 1.075 and -1.2 <= p["day_change"] <= 1.8 and p["vol_ratio_5"] <= 0.95 and 0 <= p["past5"] <= 6.5 and p["past10"] >= -3


def final_breakout(p: dict) -> bool:
    return tradeable(p, 1.8e8, 5e9, 2.0, 6.8) and p["close"] > p["bbi"] > p["ma60"] and p["bbi"] > p["bbi_3"] and p["close"] > p["prev3_high"] and p["close_bbi"] <= 1.035 and p["close_high"] >= 0.992 and p["upper_shadow"] <= 0.012 and p["close"] >= p["open"] and 2.5 <= p["day_change"] <= 4.9 and 1.20 <= p["vol_ratio_5"] <= 1.80 and p["past5"] <= 7.5 and p["past10"] <= 13


OLD_FORMULAS = {
    "趋势预备池": old_trend,
    "B1回踩池": old_b1,
    "B2回踩池": old_b2,
    "起爆确认池": old_breakout,
}

FINAL_FORMULAS = {
    "趋势精选池": final_trend,
    "B1回踩精选池": final_b1,
    "B2回踩精选池": final_b2,
    "起爆精选池": final_breakout,
}

CAPS = {
    "趋势精选池": 18,
    "B1回踩精选池": 8,
    "B2回踩精选池": 8,
    "起爆精选池": 6,
}


def score(pool: str, p: dict) -> float:
    liquidity = min(p["amt20"] / 1e8, 15) + min(p["market_cap"] / 1e8, 500) / 120
    slope = max(0, (p["bbi"] / p["bbi_3"] - 1) * 800) + max(0, (p["ma60"] / p["ma60_5"] - 1) * 500)
    if "起爆" in pool:
        return liquidity + slope + (p["close_high"] - 0.99) * 600 - abs(p["vol_ratio_5"] - 1.45) * 10 - max(0, p["past5"] - 6) * 4
    if "B1" in pool:
        return liquidity + slope - abs(p["low_ma60"] - 1) * 900 - abs(p["close_ma60"] - 1.01) * 650 - p["vol_ratio_5"] * 7
    if "B2" in pool:
        return liquidity + slope - abs(p["low_bbi"] - 1) * 1000 - abs(p["close_bbi"] - 1.006) * 800 - p["vol_ratio_5"] * 7
    return liquidity + slope - abs(p["close_bbi"] - 1.01) * 520 - abs(p["close_ma60"] - 1.04) * 150 - max(0, p["past5"] - 5) * 3


def select_rows(points: list[dict], pool: str, fn: Callable[[dict], bool], cap: bool) -> list[dict]:
    rows = [p for p in points if fn(p)]
    rows.sort(key=lambda p: score(pool, p), reverse=True)
    if cap and pool in CAPS:
        return rows[: CAPS[pool]]
    return rows


def return_stats(rows: list[dict]) -> dict:
    rows = [r for r in rows if r.get("fwd5") is not None]
    if not rows:
        return {"n": 0, "ret1": 0.0, "ret3": 0.0, "ret5": 0.0, "win3": 0.0}
    r1 = [r["fwd1"] for r in rows]
    r3 = [r["fwd3"] for r in rows]
    r5 = [r["fwd5"] for r in rows]
    return {
        "n": len(rows),
        "ret1": round(avg(r1), 3),
        "ret3": round(avg(r3), 3),
        "ret5": round(avg(r5), 3),
        "win3": round(sum(x > 0 for x in r3) / len(r3) * 100, 1),
    }


def summarize(backtest_by_date: dict[str, list[dict]], formulas: dict[str, Callable[[dict], bool]], cap: bool) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for pool, fn in formulas.items():
        counts: list[int] = []
        all_rows: list[dict] = []
        for date in sorted(backtest_by_date):
            rows = select_rows(backtest_by_date[date], pool, fn, cap)
            counts.append(len(rows))
            all_rows.extend(rows)
        out[pool] = {
            "avg_count": round(avg(counts), 2),
            "median_count": round(med(counts), 2),
            "max_count": max(counts) if counts else 0,
            **return_stats(all_rows),
        }
    return out


def current_candidates(current_by_date: dict[str, list[dict]], formulas: dict[str, Callable[[dict], bool]], cap: bool) -> dict[str, list[dict]]:
    latest = max(current_by_date)
    points = current_by_date[latest]
    return {pool: select_rows(points, pool, fn, cap) for pool, fn in formulas.items()}


def render_report(payload: dict) -> str:
    lines = [
        "# 本地轻量 BBI-MA60 回测程序运行报告",
        "",
        f"- 生成时间：{payload['run_time']}",
        "- 引入方案：`efinance` 获取全市场实时基础字段；东方财富日K JSON 接口并发获取历史行情；本地 `pandas/numpy` 思路向量化计算，不再使用慢速 baostock 单线程。",
        f"- 可用股票：{payload['universe_count']}；日K覆盖：{payload['kline_count']}。",
        f"- 当前最新交易日：{payload['current_latest_date']}。",
        f"- 回测信号截止日：{payload['backtest_signal_end']}。说明：为了计算未来5日收益，回测信号日必须比当前最新日提前5个交易日；当前池子另算，不再混淆。",
        f"- 总耗时：{payload['elapsed_sec']} 秒。",
        "",
        "## 为什么要换程序",
        "",
        "旧程序有三个问题：一是 `baostock` 同步太慢；二是旧回测还在13/34均线，和现在BBI/MA60体系不一致；三是把可回测日期和当前最新日期混在一起，容易误判当前池子。",
        "新程序先解决数据和回测口径，再谈四池公式。现在所有池子都用同一套 BBI、MA60、成交量、成交额、换手、市值字段计算。",
        "",
        "## 回测对比",
        "",
        "### 旧文档公式，未做前N截断",
        "",
        "| 池子 | 平均每日出票 | 中位数 | 最大值 | 3日均值 | 5日均值 | 3日胜率 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for pool, row in payload["old_raw"].items():
        lines.append(f"| {pool} | {row['avg_count']} | {row['median_count']} | {row['max_count']} | {row['ret3']}% | {row['ret5']}% | {row['win3']}% |")
    lines.extend(["", "### 最终精选公式 + 前N硬截断", "", "| 池子 | 平均每日出票 | 中位数 | 最大值 | 3日均值 | 5日均值 | 3日胜率 |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"])
    for pool, row in payload["final_capped"].items():
        lines.append(f"| {pool} | {row['avg_count']} | {row['median_count']} | {row['max_count']} | {row['ret3']}% | {row['ret5']}% | {row['win3']}% |")

    lines.extend(["", "## 当前最新交易日精选池", ""])
    for pool, rows in payload["current_final"].items():
        lines.extend([f"### {pool}", ""])
        if not rows:
            lines.extend(["- 当前没有满足条件的标的。", ""])
            continue
        lines.append("| 代码 | 名称 | 收盘 | 涨幅 | 距BBI | 距MA60 | 量/5 | 换手 | 20日均额(亿) | 总市值(亿) |")
        lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
        for p in rows:
            lines.append(
                f"| {p['code']} | {p['name']} | {p['close']:.2f} | {p['day_change']:.2f}% | "
                f"{(p['close_bbi'] - 1) * 100:.2f}% | {(p['close_ma60'] - 1) * 100:.2f}% | "
                f"{p['vol_ratio_5']:.2f} | {p['turnover']:.2f}% | {p['amt20']/1e8:.2f} | {p['market_cap']/1e8:.1f} |"
            )
        lines.append("")

    lines.extend(
        [
            "## GitHub/开源取舍",
            "",
            "- `AKShare`：高星、覆盖全，但本机实测实时全市场接口超过2分钟未返回，不适合放主流程。",
            "- `efinance`：轻量，实时全市场5秒返回；适合作为基础字段入口。",
            "- `backtesting.py`：轻量高星，但偏单标的策略，不适合每天横截面筛几千只A股。",
            "- `backtrader/zipline/qlib`：能力强但偏重，不适合现在这个“同花顺四池日常筛选”的轻量目标。",
            "",
            "## 当前结论",
            "",
            "现在本地回测程序已经从慢速同步改成轻量并发版。下一步才是把同花顺四句公式同步成“条件 + 排序前N”，否则四个池子在同花顺里仍可能因为识别不完整而膨胀到70只以上。",
        ]
    )
    return "\n".join(lines) + "\n"


def compact_rows(rows: list[dict]) -> list[dict]:
    keys = [
        "date",
        "code",
        "name",
        "open",
        "close",
        "high",
        "low",
        "bbi",
        "ma60",
        "day_change",
        "past5",
        "close_bbi",
        "close_ma60",
        "vol_ratio_5",
        "vol_ratio_20",
        "turnover",
        "amt20",
        "market_cap",
    ]
    return [{k: row[k] for k in keys if k in row} for row in rows]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--cache-key", default=datetime.now().strftime("%Y%m%d"))
    parser.add_argument("--beg", default="20250101")
    parser.add_argument("--end", default=datetime.now().strftime("%Y%m%d"))
    parser.add_argument("--limit", type=int, default=360)
    parser.add_argument("--lookback-days", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=80)
    args = parser.parse_args()

    started = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    universe = load_universe(refresh=args.refresh, cache_key=args.cache_key)
    klines = await load_klines(
        universe,
        refresh=args.refresh,
        cache_key=args.cache_key,
        beg=args.beg,
        end=args.end,
        limit=args.limit,
        concurrency=args.concurrency,
    )
    backtest_by_date, current_by_date, bt_end, current_latest = build_points(universe, klines, args.lookback_days)

    current_final_raw = current_candidates(current_by_date, FINAL_FORMULAS, cap=True)
    payload = {
        "run_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "universe_count": len(universe),
        "kline_count": len(klines),
        "current_latest_date": current_latest,
        "backtest_signal_end": bt_end,
        "elapsed_sec": round(time.time() - started, 2),
        "old_raw": summarize(backtest_by_date, OLD_FORMULAS, cap=False),
        "final_raw": summarize(backtest_by_date, FINAL_FORMULAS, cap=False),
        "final_capped": summarize(backtest_by_date, FINAL_FORMULAS, cap=True),
        "current_final": {pool: compact_rows(rows) for pool, rows in current_final_raw.items()},
        "current_counts": {pool: len(rows) for pool, rows in current_final_raw.items()},
    }
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = OUT_DIR / f"lightweight_bbi_backtest_report_{stamp}.md"
    json_path = OUT_DIR / f"lightweight_bbi_backtest_result_{stamp}.json"
    latest_report = OUT_DIR / "latest_lightweight_bbi_backtest_report.md"
    latest_json = OUT_DIR / "latest_lightweight_bbi_backtest_result.json"

    text = render_report(payload)
    report_path.write_text(text, encoding="utf-8")
    latest_report.write_text(text, encoding="utf-8")
    json_payload = json.dumps(payload, ensure_ascii=False, indent=2)
    json_path.write_text(json_payload, encoding="utf-8")
    latest_json.write_text(json_payload, encoding="utf-8")

    print(json.dumps(
        {
            "elapsed_sec": payload["elapsed_sec"],
            "current_latest_date": current_latest,
            "backtest_signal_end": bt_end,
            "current_counts": payload["current_counts"],
            "report": str(latest_report),
            "json": str(latest_json),
        },
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    asyncio.run(main())
