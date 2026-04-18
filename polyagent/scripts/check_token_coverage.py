#!/usr/bin/env python3
"""Diagnose token ID coverage: markets.csv vs orderFilled.csv join."""
import csv
import sys
from pathlib import Path

data_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data")

with open(data_dir / "markets.csv") as f:
    rows = list(csv.DictReader(f))

has_tokens = sum(1 for r in rows if r.get("token1", "").strip())
print(f"{has_tokens}/{len(rows)} markets have token1 populated")
print("Sample token1:", next((r["token1"] for r in rows if r.get("token1", "").strip()), "NONE"))

with open(data_dir / "goldsky" / "orderFilled.csv") as f:
    f.readline()
    print("Sample orderFilled asset:", f.readline().split(",")[2])
