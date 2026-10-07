from __future__ import annotations

import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import baostock as bs

from backtest_formulas import Candle, UniverseRow, fetch_universe


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "market_data.sqlite3"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_conn()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS universe_latest (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                price REAL NOT NULL,
                market_cap REAL NOT NULL,
                float_cap REAL NOT NULL,
                snapshot_date TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS daily_bars (
                code TEXT NOT NULL,
                date TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                preclose REAL NOT NULL,
                volume_shares REAL NOT NULL,
                amount REAL NOT NULL,
                turn REAL NOT NULL,
                pct_chg REAL NOT NULL,
                adjustflag TEXT NOT NULL,
                PRIMARY KEY (code, date)
            );

            CREATE INDEX IF NOT EXISTS idx_daily_bars_date ON daily_bars(date);

            CREATE TABLE IF NOT EXISTS min30_bars (
                code TEXT NOT NULL,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                amount REAL NOT NULL,
                PRIMARY KEY (code, date, time)
            );

            CREATE INDEX IF NOT EXISTS idx_min30_code_date ON min30_bars(code, date);
            """
        )
        conn.commit()
    finally:
        conn.close()


def next_date_str(date_str: str) -> str:
    return (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


def market_code(code: str) -> str:
    return f"sz.{code}" if code.startswith(("000", "001", "002", "300")) else f"sh.{code}"


def safe_float(value: str | float | int | None) -> float:
    if value in (None, ""):
        return 0.0
    return float(value)


async def sync_universe(snapshot_date: str | None = None) -> dict[str, UniverseRow]:
    init_db()
    if snapshot_date is None:
        snapshot_date = datetime.now().strftime("%Y-%m-%d")

    universe = await fetch_universe(refresh=True)
    conn = get_conn()
    try:
        conn.executemany(
            """
            INSERT INTO universe_latest (code, name, price, market_cap, float_cap, snapshot_date)
            VALUES (:code, :name, :price, :market_cap, :float_cap, :snapshot_date)
            ON CONFLICT(code) DO UPDATE SET
                name=excluded.name,
                price=excluded.price,
                market_cap=excluded.market_cap,
                float_cap=excluded.float_cap,
                snapshot_date=excluded.snapshot_date
            """,
            [
                {
                    **asdict(row),
                    "snapshot_date": snapshot_date,
                }
                for row in universe.values()
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return universe


def load_universe_from_db() -> dict[str, UniverseRow]:
    init_db()
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT code, name, price, market_cap, float_cap FROM universe_latest"
        ).fetchall()
    finally:
        conn.close()
    return {
        row["code"]: UniverseRow(
            code=row["code"],
            name=row["name"],
            price=row["price"],
            market_cap=row["market_cap"],
            float_cap=row["float_cap"],
        )
        for row in rows
    }


def sync_daily_bars(
    universe: dict[str, UniverseRow],
    start_date: str = "2025-12-01",
    end_date: str | None = None,
    full_refresh: bool = False,
) -> dict[str, int]:
    init_db()
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")

    conn = get_conn()
    inserted = 0
    skipped = 0

    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"baostock login failed: {login.error_msg}")

    try:
        for idx, code in enumerate(sorted(universe), start=1):
            if full_refresh:
                begin = start_date
            else:
                row = conn.execute(
                    "SELECT MAX(date) AS last_date FROM daily_bars WHERE code = ?",
                    (code,),
                ).fetchone()
                last_date = row["last_date"] if row else None
                begin = next_date_str(last_date) if last_date else start_date

            if begin > end_date:
                skipped += 1
                continue

            rs = bs.query_history_k_data_plus(
                market_code(code),
                "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,pctChg",
                start_date=begin,
                end_date=end_date,
                frequency="d",
                adjustflag="2",
            )
            batch = []
            while rs.error_code == "0" and rs.next():
                date, _code, o, h, l, c, preclose, volume, amount, adjustflag, turn, pct_chg = rs.get_row_data()
                batch.append(
                    (
                        code,
                        date,
                        safe_float(o),
                        safe_float(h),
                        safe_float(l),
                        safe_float(c),
                        safe_float(preclose),
                        safe_float(volume),
                        safe_float(amount),
                        safe_float(turn),
                        safe_float(pct_chg),
                        adjustflag,
                    )
                )

            if batch:
                conn.executemany(
                    """
                    INSERT INTO daily_bars
                    (code, date, open, high, low, close, preclose, volume_shares, amount, turn, pct_chg, adjustflag)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(code, date) DO UPDATE SET
                        open=excluded.open,
                        high=excluded.high,
                        low=excluded.low,
                        close=excluded.close,
                        preclose=excluded.preclose,
                        volume_shares=excluded.volume_shares,
                        amount=excluded.amount,
                        turn=excluded.turn,
                        pct_chg=excluded.pct_chg,
                        adjustflag=excluded.adjustflag
                    """,
                    batch,
                )
                inserted += len(batch)

            if idx % 50 == 0:
                conn.commit()

        conn.commit()
    finally:
        bs.logout()
        conn.close()

    return {"inserted_rows": inserted, "skipped_codes": skipped, "codes": len(universe)}


def load_daily_klines_from_db(
    universe: dict[str, UniverseRow],
    start_date: str = "2025-12-01",
) -> dict[str, list[Candle]]:
    init_db()
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT code, date, open, high, low, close, volume_shares, amount
            FROM daily_bars
            WHERE date >= ?
            ORDER BY code, date
            """,
            (start_date,),
        ).fetchall()
    finally:
        conn.close()

    out: dict[str, list[Candle]] = {}
    for row in rows:
        code = row["code"]
        if code not in universe:
            continue
        out.setdefault(code, []).append(
            Candle(
                date=row["date"],
                open=row["open"],
                close=row["close"],
                high=row["high"],
                low=row["low"],
                volume_hands=row["volume_shares"] / 100.0,
                amount=row["amount"],
            )
        )
    return out


def sync_min30_for_codes(
    codes: Iterable[str],
    start_date: str,
    end_date: str,
    refresh: bool = False,
) -> dict[str, int]:
    init_db()
    conn = get_conn()
    codes = sorted(set(codes))
    inserted = 0

    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"baostock login failed: {login.error_msg}")
    try:
        for idx, code in enumerate(codes, start=1):
            if refresh:
                conn.execute(
                    "DELETE FROM min30_bars WHERE code = ? AND date BETWEEN ? AND ?",
                    (code, start_date, end_date),
                )

            rs = bs.query_history_k_data_plus(
                market_code(code),
                "date,time,open,high,low,close,volume,amount",
                start_date=start_date,
                end_date=end_date,
                frequency="30",
                adjustflag="2",
            )
            batch = []
            while rs.error_code == "0" and rs.next():
                date, time_raw, o, h, l, c, volume, amount = rs.get_row_data()
                batch.append(
                    (
                        code,
                        date,
                        time_raw[-9:-3],
                        safe_float(o),
                        safe_float(h),
                        safe_float(l),
                        safe_float(c),
                        safe_float(volume),
                        safe_float(amount),
                    )
                )
            if batch:
                conn.executemany(
                    """
                    INSERT INTO min30_bars
                    (code, date, time, open, high, low, close, volume, amount)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(code, date, time) DO UPDATE SET
                        open=excluded.open,
                        high=excluded.high,
                        low=excluded.low,
                        close=excluded.close,
                        volume=excluded.volume,
                        amount=excluded.amount
                    """,
                    batch,
                )
                inserted += len(batch)

            if idx % 20 == 0:
                conn.commit()

        conn.commit()
    finally:
        bs.logout()
        conn.close()

    return {"codes": len(codes), "inserted_rows": inserted}


def load_min30_from_db(codes: Iterable[str], start_date: str, end_date: str) -> dict[str, list[object]]:
    init_db()
    codes = list(codes)
    if not codes:
        return {}
    conn = get_conn()
    try:
        placeholders = ",".join("?" for _ in codes)
        params = [*codes, start_date, end_date]
        rows = conn.execute(
            f"""
            SELECT code, date, time, open, high, low, close, volume, amount
            FROM min30_bars
            WHERE code IN ({placeholders}) AND date BETWEEN ? AND ?
            ORDER BY code, date, time
            """,
            params,
        ).fetchall()
    finally:
        conn.close()

    from backtest_30m_execution import MinBar

    out: dict[str, list[object]] = {}
    for row in rows:
        out.setdefault(row["code"], []).append(
            MinBar(
                date=row["date"],
                time=row["time"],
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"],
                amount=row["amount"],
            )
        )
    return out
