from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import aiohttp


ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "cache" / "formula_backtest"
UNIVERSE_PATH = CACHE_DIR / "universe_latest.json"
KLINE_PATH = CACHE_DIR / "klines_latest.json"
REPORT_PATH = ROOT / "output" / "formula_backtest_report.md"

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

    @property
    def total_shares(self) -> float:
        return self.market_cap / self.price if self.market_cap > 0 and self.price > 0 else 0.0

    @property
    def float_shares(self) -> float:
        return self.float_cap / self.price if self.float_cap > 0 and self.price > 0 else 0.0


@dataclass
class Candle:
    date: str
    open: float
    close: float
    high: float
    low: float
    volume_hands: float
    amount: float


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


async def fetch_json(session: aiohttp.ClientSession, url: str, params: dict) -> dict | None:
    for _ in range(3):
        try:
            async with session.get(
                url,
                params=params,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                return await resp.json(content_type=None)
        except Exception:
            continue
    return None


async def fetch_universe(refresh: bool = False) -> dict[str, UniverseRow]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if UNIVERSE_PATH.exists() and not refresh:
        raw = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
        return {code: UniverseRow(**row) for code, row in raw.items()}

    params_base = {
        "pn": "1",
        "pz": "100",
        "po": "1",
        "np": "1",
        "fltt": "2",
        "invt": "2",
        "fid": "f3",
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
        "fields": "f12,f14,f2,f20,f21",
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
    }

    connector = aiohttp.TCPConnector(limit=8, ssl=False)
    async with aiohttp.ClientSession(headers=HEADERS, trust_env=False, connector=connector) as session:
        first_payload = await fetch_json(session, "https://push2.eastmoney.com/api/qt/clist/get", params_base)
        total = int((((first_payload or {}).get("data") or {}).get("total")) or 0)
        page_count = max(1, math.ceil(total / int(params_base["pz"])))

        payloads = [first_payload]
        tasks = []
        for page in range(2, page_count + 1):
            params = dict(params_base)
            params["pn"] = str(page)
            tasks.append(fetch_json(session, "https://push2.eastmoney.com/api/qt/clist/get", params))
        if tasks:
            payloads.extend(await asyncio.gather(*tasks))

    out: dict[str, UniverseRow] = {}
    for payload in payloads:
        rows = (((payload or {}).get("data") or {}).get("diff")) or []
        for row in rows:
            try:
                code = str(row["f12"])
                name = str(row["f14"])
                price = float(row["f2"] or 0)
                market_cap = float(row["f20"] or 0)
                float_cap = float(row["f21"] or 0)
            except Exception:
                continue
            if not code.startswith(MAINBOARD_PREFIXES):
                continue
            if any(part in name for part in BLOCKED_NAME_PARTS):
                continue
            if price <= 0:
                continue
            out[code] = UniverseRow(
                code=code,
                name=name,
                price=price,
                market_cap=market_cap,
                float_cap=float_cap,
            )

    UNIVERSE_PATH.write_text(
        json.dumps({code: asdict(row) for code, row in out.items()}, ensure_ascii=False),
        encoding="utf-8",
    )
    return out


async def fetch_one_kline(session: aiohttp.ClientSession, code: str) -> tuple[str, list[Candle] | None]:
    market = "0" if code.startswith(("000", "001", "002", "300")) else "1"
    params = {
        "secid": f"{market}.{code}",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": "1",
        "beg": "20251201",
        "end": datetime.now().strftime("%Y%m%d"),
        "lmt": "220",
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
    }
    payload = await fetch_json(session, "https://push2his.eastmoney.com/api/qt/stock/kline/get", params)
    rows = (((payload or {}).get("data") or {}).get("klines")) or []
    if not rows:
        return code, None

    candles = []
    for line in rows:
        d, o, c, h, l, v, amt, *_ = line.split(",")
        candles.append(
            Candle(
                date=d,
                open=float(o),
                close=float(c),
                high=float(h),
                low=float(l),
                volume_hands=float(v),
                amount=float(amt),
            )
        )
    return code, candles


async def fetch_klines(universe: dict[str, UniverseRow], refresh: bool = False) -> dict[str, list[Candle]]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if KLINE_PATH.exists() and not refresh:
        raw = json.loads(KLINE_PATH.read_text(encoding="utf-8"))
        return {code: [Candle(**row) for row in rows] for code, rows in raw.items()}

    connector = aiohttp.TCPConnector(limit=30, ssl=False)
    async with aiohttp.ClientSession(headers=HEADERS, trust_env=False, connector=connector) as session:
        tasks = [fetch_one_kline(session, code) for code in universe]
        results = await asyncio.gather(*tasks)

    out: dict[str, list[Candle]] = {}
    for code, candles in results:
        if candles:
            out[code] = candles

    KLINE_PATH.write_text(
        json.dumps(
            {code: [asdict(candle) for candle in candles] for code, candles in out.items()},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return out


def formula_trend(p: dict) -> bool:
    return (
        p["close"] > p["ma13"]
        and p["ma13"] > p["ma34"]
        and safe_div(p["ma13"], p["ma34"]) <= 1.06
        and safe_div(p["close"], p["ma13"]) <= 1.06
        and safe_div(p["close"], p["ma34"]) <= 1.11
        and 0.0 <= p["past5"] <= 14.0
        and 0.7 <= p["vol_ratio_20"] <= 2.0
        and 1.2 <= p["turnover"] <= 8.0
        and p["amt20"] >= 8e7
        and p["market_cap"] >= 3.3e9
    )


def formula_b1(p: dict) -> bool:
    return (
        p["close"] > p["ma34"]
        and p["ma13"] > p["ma34"]
        and safe_div(p["ma13"], p["ma34"]) <= 1.05
        and 0.97 <= safe_div(p["low"], p["ma34"]) <= 1.02
        and safe_div(p["close"], p["ma34"]) <= 1.05
        and safe_div(p["close"], p["ma13"]) <= 1.03
        and p["close"] >= p["open"]
        and p["vol_ratio_5"] <= 1.05
        and p["day_change"] <= 1.5
        and p["past5"] <= 8.0
        and 1.2 <= p["turnover"] <= 7.0
        and p["amt20"] >= 1e8
        and p["market_cap"] >= 3.3e9
    )


def formula_b2(p: dict) -> bool:
    return (
        p["close"] > p["ma13"]
        and p["ma13"] > p["ma34"]
        and 1.005 <= safe_div(p["ma13"], p["ma34"]) <= 1.055
        and 0.97 <= safe_div(p["low"], p["ma13"]) <= 1.01
        and 1.0 <= safe_div(p["close"], p["ma13"]) <= 1.03
        and safe_div(p["close"], p["ma34"]) <= 1.08
        and p["close"] >= p["open"]
        and p["vol_ratio_5"] <= 1.08
        and p["day_change"] <= 3.0
        and 1.0 <= p["past5"] <= 10.0
        and 1.5 <= p["turnover"] <= 7.0
        and p["amt20"] >= 1e8
        and p["market_cap"] >= 3.3e9
    )


def formula_breakout(p: dict) -> bool:
    return (
        p["close"] > p["ma13"]
        and p["ma13"] > p["ma34"]
        and p["close"] > p["prev_high"]
        and safe_div(p["close"], p["ma13"]) <= 1.05
        and safe_div(p["close"], p["high"]) >= 0.985
        and p["close"] >= p["open"]
        and 1.8 <= p["day_change"] <= 5.8
        and 1.05 <= p["vol_ratio_5"] <= 2.3
        and p["past5"] <= 12.0
        and 1.5 <= p["turnover"] <= 8.0
        and p["amt20"] >= 1e8
        and p["market_cap"] >= 3.3e9
    )


FORMULAS = {
    "趋势预备池": formula_trend,
    "B1回踩池": formula_b1,
    "B2回踩池": formula_b2,
    "起爆确认池": formula_breakout,
}


def build_points(universe: dict[str, UniverseRow], klines: dict[str, list[Candle]], lookback_days: int) -> dict[str, list[dict]]:
    by_date: dict[str, list[dict]] = {}
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

        start = max(34, len(candles) - lookback_days - 5)
        end = len(candles) - 5

        for i in range(start, end):
            ma13 = avg(closes[i - 12 : i + 1])
            ma34 = avg(closes[i - 33 : i + 1])
            vma5 = avg(vols[i - 4 : i + 1])
            vma20 = avg(vols[i - 19 : i + 1])
            amt20 = avg(amts[i - 19 : i + 1])
            market_cap = meta.total_shares * closes[i] if meta.total_shares else 0.0
            turnover = vols[i] * 100.0 / meta.float_shares * 100.0 if meta.float_shares else 0.0

            by_date.setdefault(candles[i].date, []).append(
                {
                    "code": code,
                    "close": closes[i],
                    "open": opens[i],
                    "low": lows[i],
                    "high": highs[i],
                    "ma13": ma13,
                    "ma34": ma34,
                    "vol_ratio_5": safe_div(vols[i], vma5),
                    "vol_ratio_20": safe_div(vols[i], vma20),
                    "amt20": amt20,
                    "day_change": pct_change(closes[i], closes[i - 1]),
                    "past5": pct_change(closes[i], closes[i - 5]),
                    "turnover": turnover,
                    "market_cap": market_cap,
                    "prev_high": highs[i - 1],
                    "fwd1": pct_change(closes[i + 1], closes[i]),
                    "fwd3": pct_change(closes[i + 3], closes[i]),
                    "fwd5": pct_change(closes[i + 5], closes[i]),
                }
            )
    return by_date


def summarize_rows(rows: list[dict]) -> dict:
    if not rows:
        return {
            "ret1": 0.0,
            "ret3": 0.0,
            "ret5": 0.0,
            "win1": 0.0,
            "win3": 0.0,
            "win5": 0.0,
        }
    ret1 = [row["fwd1"] for row in rows]
    ret3 = [row["fwd3"] for row in rows]
    ret5 = [row["fwd5"] for row in rows]
    return {
        "ret1": round(avg(ret1), 3),
        "ret3": round(avg(ret3), 3),
        "ret5": round(avg(ret5), 3),
        "win1": round(sum(x > 0 for x in ret1) / len(ret1) * 100, 1),
        "win3": round(sum(x > 0 for x in ret3) / len(ret3) * 100, 1),
        "win5": round(sum(x > 0 for x in ret5) / len(ret5) * 100, 1),
    }


def backtest(universe: dict[str, UniverseRow], klines: dict[str, list[Candle]], lookback_days: int) -> tuple[dict[str, dict], dict]:
    by_date = build_points(universe, klines, lookback_days)

    stats = {
        name: {"counts": [], "ret1": [], "ret3": [], "ret5": [], "win1": [], "win3": [], "win5": []}
        for name in FORMULAS
    }
    benchmark_rows: list[dict] = []

    for date in sorted(by_date):
        points = by_date[date]
        benchmark_rows.extend(points)

        grouped = {name: [] for name in FORMULAS}
        for p in points:
            for name, fn in FORMULAS.items():
                if fn(p):
                    grouped[name].append(p)

        for name, rows in grouped.items():
            stats[name]["counts"].append(len(rows))
            if rows:
                summary = summarize_rows(rows)
                stats[name]["ret1"].append(summary["ret1"])
                stats[name]["ret3"].append(summary["ret3"])
                stats[name]["ret5"].append(summary["ret5"])
                stats[name]["win1"].append(summary["win1"])
                stats[name]["win3"].append(summary["win3"])
                stats[name]["win5"].append(summary["win5"])

    out: dict[str, dict] = {}
    for name, row in stats.items():
        counts = row["counts"]
        out[name] = {
            "days": len(counts),
            "avg_count": round(avg(counts), 2) if counts else 0.0,
            "median_count": statistics.median(counts) if counts else 0.0,
            "max_count": max(counts) if counts else 0,
            "avg_ret1": round(avg(row["ret1"]), 3) if row["ret1"] else 0.0,
            "avg_ret3": round(avg(row["ret3"]), 3) if row["ret3"] else 0.0,
            "avg_ret5": round(avg(row["ret5"]), 3) if row["ret5"] else 0.0,
            "avg_win1": round(avg(row["win1"]), 1) if row["win1"] else 0.0,
            "avg_win3": round(avg(row["win3"]), 1) if row["win3"] else 0.0,
            "avg_win5": round(avg(row["win5"]), 1) if row["win5"] else 0.0,
        }

    benchmark = summarize_rows(benchmark_rows)
    benchmark["sample_points"] = len(benchmark_rows)
    return out, benchmark


def write_report(summary: dict[str, dict], benchmark: dict, lookback_days: int) -> None:
    lines = [
        "# 公式回测报告",
        "",
        f"回测窗口：最近 {lookback_days} 个交易日",
        "",
        "说明：",
        "- 标的范围：沪深主板非 ST。",
        "- 历史总市值、换手率使用当前总股本/流通股本近似回推，更适合做公式对比，不适合做精确归因。",
        "- 收益为入选当日收盘后观察后续 1 日、3 日、5 日的等权平均涨跌幅。",
        "- 本报告更适合回答“哪个池子更像观察池、哪个池子更像执行池”，不适合直接替代次日 60 分钟确认。",
        "",
        "## 市场基准",
        "",
        f"- 样本点数：{benchmark['sample_points']}",
        f"- 次日均值：{benchmark['ret1']}%",
        f"- 3日均值：{benchmark['ret3']}%",
        f"- 5日均值：{benchmark['ret5']}%",
        f"- 次日胜率：{benchmark['win1']}%",
        f"- 3日胜率：{benchmark['win3']}%",
        f"- 5日胜率：{benchmark['win5']}%",
        "",
        "## 四池结果",
        "",
        "| 池子 | 平均每日出票 | 中位数 | 最大值 | 次日均值 | 3日均值 | 5日均值 | 次日胜率 | 3日胜率 | 5日胜率 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, row in summary.items():
        lines.append(
            f"| {name} | {row['avg_count']} | {row['median_count']} | {row['max_count']} | "
            f"{row['avg_ret1']}% | {row['avg_ret3']}% | {row['avg_ret5']}% | "
            f"{row['avg_win1']}% | {row['avg_win3']}% | {row['avg_win5']}% |"
        )

    lines.extend(
        [
            "",
            "## 当前结论",
            "",
            "- 趋势预备池、B1 回踩池、B2 回踩池更适合当作候选池或观察池，不能只凭日线公式直接买。",
            "- 起爆确认池负责寻找突破倾向，但仍然必须等待次日 60 分钟继续承接，不能在高开冲动时追买。",
            "- 后续继续优化时，优先观察“收盘成交量公式 + 次日 60 分钟确认”的组合表现，而不是单独用日线公式判断胜负。",
            "",
        ]
    )
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=60)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    universe = await fetch_universe(refresh=args.refresh)
    klines = await fetch_klines(universe, refresh=args.refresh)
    summary, benchmark = backtest(universe, klines, args.lookback_days)
    write_report(summary, benchmark, args.lookback_days)
    print(json.dumps({"benchmark": benchmark, "formulas": summary}, ensure_ascii=False, indent=2))
    print(f"报告已写入: {REPORT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
