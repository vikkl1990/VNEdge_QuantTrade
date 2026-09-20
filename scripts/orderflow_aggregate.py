#!/usr/bin/env python3
"""
orderflow_aggregate.py — Delta futures trade archives -> 1-minute order-flow bars.

Input: Delta monthly trade CSVs (zipped or not) with columns
  product_symbol, price, size, timestamp, buyer_role   (buyer_role: taker|maker)
buyer_role == "taker" -> the BUYER was the aggressor (buy-initiated trade);
buyer_role == "maker" -> the seller was the aggressor (sell-initiated trade).

Output per symbol: storage/research/orderflow/{SYM}_1m.csv.gz with, per minute:
  open high low close vwap
  vol buy_vol sell_vol n n_buy n_sell
  big_buy_vol big_sell_vol   (trades with size >= the month's 95th percentile)
  max_size

  python scripts/orderflow_aggregate.py --glob "/Users/scorpion/Downloads/futures-trades-monthly-*"
"""
from __future__ import annotations

import argparse
import glob
import io
import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = PROJECT_ROOT / "storage" / "research" / "orderflow"
CHUNK = 2_000_000


def open_csv(path: str):
    if path.endswith(".zip"):
        z = zipfile.ZipFile(path)
        name = [n for n in z.namelist() if n.endswith(".csv")][0]
        return io.TextIOWrapper(z.open(name), encoding="utf-8")
    return open(path, "r", encoding="utf-8")


def aggregate_file(path: str) -> tuple[str, pd.DataFrame]:
    sym = None
    parts = []
    # first pass: 95th percentile of size for this file (sampled from the first chunk)
    big_thr = None
    with open_csv(path) as fh:
        for chunk in pd.read_csv(fh, usecols=["product_symbol", "price", "size", "timestamp", "buyer_role"],
                                 dtype={"product_symbol": "category", "price": "float64", "size": "float64", "buyer_role": "category"},
                                 chunksize=CHUNK):
            if sym is None:
                sym = str(chunk["product_symbol"].iloc[0])
            if big_thr is None:
                big_thr = float(chunk["size"].quantile(0.95))
            ts = pd.to_datetime(chunk["timestamp"], format="%Y-%m-%d %H:%M:%S.%f", errors="coerce")
            ts = ts.fillna(pd.to_datetime(chunk["timestamp"], errors="coerce"))
            m = ts.dt.floor("min")
            buy = (chunk["buyer_role"] == "taker").values
            size = chunk["size"].values
            price = chunk["price"].values
            big = size >= big_thr
            d = pd.DataFrame({
                "m": m.values, "price": price, "size": size,
                "buy_vol": np.where(buy, size, 0.0), "sell_vol": np.where(~buy, size, 0.0),
                "n_buy": buy.astype(np.int32), "n_sell": (~buy).astype(np.int32),
                "big_buy": np.where(buy & big, size, 0.0), "big_sell": np.where(~buy & big, size, 0.0),
                "pv": price * size,
            })
            g = d.groupby("m", sort=True)
            agg = pd.DataFrame({
                "open": g["price"].first(), "high": g["price"].max(), "low": g["price"].min(), "close": g["price"].last(),
                "pv": g["pv"].sum(), "vol": g["size"].sum(), "buy_vol": g["buy_vol"].sum(), "sell_vol": g["sell_vol"].sum(),
                "n": g.size(), "n_buy": g["n_buy"].sum(), "n_sell": g["n_sell"].sum(),
                "big_buy_vol": g["big_buy"].sum(), "big_sell_vol": g["big_sell"].sum(), "max_size": g["size"].max(),
            })
            parts.append(agg)
    df = pd.concat(parts)
    # a minute can straddle two chunks: re-aggregate duplicates
    if df.index.has_duplicates:
        g = df.groupby(level=0, sort=True)
        df = pd.DataFrame({
            "open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(), "close": g["close"].last(),
            "pv": g["pv"].sum(), "vol": g["vol"].sum(), "buy_vol": g["buy_vol"].sum(), "sell_vol": g["sell_vol"].sum(),
            "n": g["n"].sum(), "n_buy": g["n_buy"].sum(), "n_sell": g["n_sell"].sum(),
            "big_buy_vol": g["big_buy_vol"].sum(), "big_sell_vol": g["big_sell_vol"].sum(), "max_size": g["max_size"].max(),
        })
    df["vwap"] = df["pv"] / df["vol"].replace(0, np.nan)
    df = df.drop(columns=["pv"])
    df.index.name = "minute"
    df["big_thr"] = big_thr
    return sym, df


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--glob", default="/Users/scorpion/Downloads/futures-trades-monthly-*")
    args = ap.parse_args()
    files = sorted(set(glob.glob(args.glob)))
    # prefer .zip when both a zip and a loose csv exist for the same month
    keep = {}
    for f in files:
        key = Path(f).name.replace(" (1)", "").replace(".zip", "")
        if key not in keep or f.endswith(".zip"):
            keep[key] = f
    files = sorted(keep.values())
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    by_sym: dict[str, list] = {}
    for f in files:
        print(f"aggregating {Path(f).name} ...", flush=True)
        sym, df = aggregate_file(f)
        print(f"  {sym}: {len(df)} minutes, {int(df['n'].sum())} trades, big_thr={df['big_thr'].iloc[0]:.0f}", flush=True)
        by_sym.setdefault(sym, []).append(df)
    for sym, parts in by_sym.items():
        allm = pd.concat(parts).sort_index()
        allm = allm[~allm.index.duplicated(keep="last")]
        out = OUT_DIR / f"{sym}_1m.csv.gz"
        allm.to_csv(out, compression="gzip")
        print(f"wrote {out}: {len(allm)} minutes {allm.index.min()} -> {allm.index.max()}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
