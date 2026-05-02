"""Tests for the registry-aware short-horizon decider."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from polyagent.services.bankroll import BankrollState
from polyagent.services.quant.short_horizon.decider import QuantDecider


def _bankroll(starting="20", realized="0", open_main="0", open_quant="0"):
    return BankrollState(
        starting=Decimal(str(starting)),
        realized_main=Decimal(str(realized)),
        realized_quant=Decimal("0"),
        open_capital_main=Decimal(str(open_main)),
        open_capital_quant=Decimal(str(open_quant)),
    )


class _FakeRepo:
    def __init__(self, open_per_asset: dict[str, int] | None = None):
        self.trades_for: dict[str, list] = {}
        self.inserted: list = []
        self.open_per_asset = open_per_asset or {}
        self.start_spots: dict[str, object] = {}

    def get_trades_for_market(self, market_id):
        return self.trades_for.get(market_id, [])

    def insert_trade(self, t):
        self.inserted.append(t)
        # Mirror DB-side accounting so per-asset cap works across calls.
        # Trades are unresolved at insertion time; map by inferred asset.
        # We can't read asset_id off the trade, so callers update
        # self.open_per_asset directly when needed.

    def count_open_trades_for_asset(self, asset_id: str) -> int:
        return self.open_per_asset.get(asset_id, 0)

    def set_start_spot(self, market_id, start_spot):
        self.start_spots[market_id] = start_spot


class _FakeBook:
    def __init__(self, bid_ask):
        self._b = bid_ask

    def fetch_mid(self, token_id):
        return self._b


class _FakeSrc:
    def __init__(self, cur):
        self._c = cur

    def current(self):
        return self._c

    def realized_vol(self, window_s):
        return 0.0


class _FakeSettlement:
    def __init__(self, price):
        self._price = price

    def price_at(self, ts):
        return self._price


def _row(asset_id="BTC"):
    now = datetime.now(timezone.utc)
    return {
        "id": "m1",
        "polymarket_id": "0xabc",
        "asset_id": asset_id,
        "token_id_yes": "yes_id",
        "window_start_ts": now - timedelta(seconds=60),
        "window_end_ts": now + timedelta(seconds=120),
        "start_spot": None,
    }


def test_decider_inserts_paper_trade_when_edge_clears():
    repo = _FakeRepo()
    sources = {"BTC": _FakeSrc(Decimal("60000"))}
    book = _FakeBook((Decimal("0.30"), Decimal("0.32")))
    d = QuantDecider(sources=sources, book=book, repo=repo, position_size_usd=Decimal("5"))
    d.evaluate(_row())
    assert len(repo.inserted) == 1
    assert repo.inserted[0].side == "YES"


def test_decider_skips_market_with_existing_trade():
    repo = _FakeRepo()
    repo.trades_for["m1"] = [{"id": "t1"}]
    sources = {"BTC": _FakeSrc(Decimal("60000"))}
    d = QuantDecider(
        sources=sources,
        book=_FakeBook((Decimal("0.4"), Decimal("0.5"))),
        repo=repo,
        position_size_usd=Decimal("5"),
    )
    d.evaluate(_row())
    assert repo.inserted == []


def test_decider_skips_when_no_spot():
    repo = _FakeRepo()
    sources = {"BTC": _FakeSrc(None)}
    d = QuantDecider(
        sources=sources,
        book=_FakeBook((Decimal("0.4"), Decimal("0.5"))),
        repo=repo,
        position_size_usd=Decimal("5"),
    )
    d.evaluate(_row())
    assert repo.inserted == []


def test_decider_skips_unknown_asset():
    repo = _FakeRepo()
    sources = {"BTC": _FakeSrc(Decimal("60000"))}
    d = QuantDecider(
        sources=sources,
        book=_FakeBook((Decimal("0.4"), Decimal("0.5"))),
        repo=repo,
        position_size_usd=Decimal("5"),
    )
    d.evaluate(_row(asset_id="DOGE"))
    assert repo.inserted == []


def test_decider_skips_when_window_closed():
    repo = _FakeRepo()
    row = _row()
    row["window_end_ts"] = datetime.now(timezone.utc) - timedelta(seconds=10)
    sources = {"BTC": _FakeSrc(Decimal("60000"))}
    d = QuantDecider(
        sources=sources,
        book=_FakeBook((Decimal("0.4"), Decimal("0.5"))),
        repo=repo,
        position_size_usd=Decimal("5"),
    )
    d.evaluate(row)
    assert repo.inserted == []


def test_decider_skips_when_edge_below_threshold():
    repo = _FakeRepo()
    sources = {"BTC": _FakeSrc(Decimal("60000"))}
    # Mid ~0.5 and p_up should be ~0.5 (no drift), edge ~ 0
    book = _FakeBook((Decimal("0.49"), Decimal("0.51")))
    d = QuantDecider(sources=sources, book=book, repo=repo, position_size_usd=Decimal("5"))
    d.evaluate(_row())
    assert repo.inserted == []


def test_decider_caps_trades_per_cycle():
    """Per-cycle cap stops the cascade where one Coinbase tick triggers
    correlated trades on every active market in the same orchestrator pass."""
    repo = _FakeRepo()
    sources = {"BTC": _FakeSrc(Decimal("60000"))}
    book = _FakeBook((Decimal("0.30"), Decimal("0.32")))  # +0.19 edge, fires
    d = QuantDecider(
        sources=sources, book=book, repo=repo,
        position_size_usd=Decimal("5"),
        max_trades_per_cycle=2,
        max_open_per_asset=99,  # disable the per-asset cap for this test
    )
    for i in range(10):
        row = _row()
        row["id"] = f"m{i}"
        row["polymarket_id"] = f"0x{i:040x}"
        d.evaluate(row)
    assert len(repo.inserted) == 2


def test_opened_this_cycle_reports_inserts_since_reset():
    """The orchestrator reads this to log how many trades the cycle just
    placed. Must reflect inserts since the most recent reset_cycle()."""
    repo = _FakeRepo()
    sources = {"BTC": _FakeSrc(Decimal("60000"))}
    book = _FakeBook((Decimal("0.30"), Decimal("0.32")))
    d = QuantDecider(sources=sources, book=book, repo=repo, position_size_usd=Decimal("5"))

    assert d.opened_this_cycle == 0
    d.evaluate(_row())
    assert d.opened_this_cycle == 1
    d.reset_cycle()
    assert d.opened_this_cycle == 0


def test_reset_cycle_clears_per_cycle_counter():
    repo = _FakeRepo()
    sources = {"BTC": _FakeSrc(Decimal("60000"))}
    book = _FakeBook((Decimal("0.30"), Decimal("0.32")))
    d = QuantDecider(
        sources=sources, book=book, repo=repo,
        position_size_usd=Decimal("5"),
        max_trades_per_cycle=2,
        max_open_per_asset=99,
    )
    for i in range(3):
        row = _row()
        row["id"] = f"a{i}"
        row["polymarket_id"] = f"0x{i:040x}"
        d.evaluate(row)
    assert len(repo.inserted) == 2

    d.reset_cycle()
    for i in range(3):
        row = _row()
        row["id"] = f"b{i}"
        row["polymarket_id"] = f"0x{(i + 100):040x}"
        d.evaluate(row)
    assert len(repo.inserted) == 4   # 2 from cycle 1 + 2 from cycle 2


def test_decider_caps_open_trades_per_asset():
    """When max_open_per_asset already-open trades exist for an asset,
    the decider refuses to open more even if edge clears the threshold."""
    repo = _FakeRepo(open_per_asset={"BTC": 3})
    sources = {"BTC": _FakeSrc(Decimal("60000"))}
    book = _FakeBook((Decimal("0.30"), Decimal("0.32")))
    d = QuantDecider(
        sources=sources, book=book, repo=repo,
        position_size_usd=Decimal("5"),
        max_trades_per_cycle=99,
        max_open_per_asset=3,
    )
    d.evaluate(_row())
    assert repo.inserted == []


def test_decider_allows_trade_when_open_under_per_asset_cap():
    repo = _FakeRepo(open_per_asset={"BTC": 2})
    sources = {"BTC": _FakeSrc(Decimal("60000"))}
    book = _FakeBook((Decimal("0.30"), Decimal("0.32")))
    d = QuantDecider(
        sources=sources, book=book, repo=repo,
        position_size_usd=Decimal("5"),
        max_trades_per_cycle=99,
        max_open_per_asset=3,
    )
    d.evaluate(_row())
    assert len(repo.inserted) == 1


def test_decider_fetches_and_persists_start_spot_when_null():
    """When start_spot is NULL in the DB, decider fetches it from the settlement
    source using window_start_ts and persists it so subsequent passes use real K."""
    repo = _FakeRepo()
    spot = Decimal("94000")
    # Settlement returns the opening price (different from current spot).
    # With start=93000 and current=94000, BTC is up 1% → p_up near 1.
    # Book mid 0.50 → edge near 0.50 → trade fires.
    opening_price = Decimal("93000")
    sources = {"BTC": _FakeSrc(spot)}
    book = _FakeBook((Decimal("0.49"), Decimal("0.51")))
    settlements = {"BTC": _FakeSettlement(opening_price)}
    d = QuantDecider(
        sources=sources, book=book, repo=repo,
        position_size_usd=Decimal("5"),
        settlements=settlements,
    )
    d.evaluate(_row())
    assert repo.start_spots.get("m1") == opening_price
    assert len(repo.inserted) == 1


def test_decider_falls_back_to_spot_when_settlement_returns_none():
    """If settlement.price_at returns None (e.g. candle unavailable),
    the decider falls back to current spot as start_spot and does not persist."""
    repo = _FakeRepo()
    sources = {"BTC": _FakeSrc(Decimal("60000"))}
    book = _FakeBook((Decimal("0.49"), Decimal("0.51")))
    settlements = {"BTC": _FakeSettlement(None)}
    d = QuantDecider(
        sources=sources, book=book, repo=repo,
        position_size_usd=Decimal("5"),
        settlements=settlements,
    )
    d.evaluate(_row())
    assert "m1" not in repo.start_spots
    assert repo.inserted == []  # mid=0.50, start=spot → p_up≈0.5 → edge<0.05


def test_decider_uses_existing_start_spot_without_settlement_call():
    """If start_spot is already set in the market row, no settlement fetch occurs."""
    repo = _FakeRepo()
    spot = Decimal("94000")
    # start_spot pre-loaded: 93000. Price moved up → p_up near 1.
    sources = {"BTC": _FakeSrc(spot)}
    book = _FakeBook((Decimal("0.49"), Decimal("0.51")))
    settlement = _FakeSettlement(Decimal("99999"))  # would give wrong price if called
    d = QuantDecider(
        sources=sources, book=book, repo=repo,
        position_size_usd=Decimal("5"),
        settlements={"BTC": settlement},
    )
    row = _row()
    row["start_spot"] = Decimal("93000")
    d.evaluate(row)
    assert "m1" not in repo.start_spots  # set_start_spot not called
    assert len(repo.inserted) == 1


# Skip-log tests: lock the format that operators grep against.
# `grep SKIP output.log | grep "reason=<code>"` must keep working.

class TestSkipLogging:
    def _decider(self, repo, *, sources=None, book=None, settlements=None,
                 max_per_cycle=5, max_open_per_asset=3, size="5"):
        return QuantDecider(
            sources=sources or {"BTC": _FakeSrc(Decimal("60000"))},
            book=book or _FakeBook((Decimal("0.49"), Decimal("0.51"))),
            repo=repo,
            position_size_usd=Decimal(size),
            max_trades_per_cycle=max_per_cycle,
            max_open_per_asset=max_open_per_asset,
            settlements=settlements,
        )

    def _row_with_slug(self, slug="btc-updown-5m-1700000000"):
        row = _row()
        row["slug"] = slug
        return row

    def test_already_traded_is_silent(self, caplog):
        """The most common skip reason. Logging it would dominate the log."""
        repo = _FakeRepo()
        repo.trades_for["m1"] = [{"id": "t1"}]
        d = self._decider(repo)
        with caplog.at_level("INFO", logger="polyagent.services.quant.short_horizon.decider"):
            d.evaluate(self._row_with_slug())
        assert not any("SKIP" in r.message for r in caplog.records)

    def test_open_cap_logged(self, caplog):
        repo = _FakeRepo(open_per_asset={"BTC": 3})
        d = self._decider(repo, max_open_per_asset=3)
        with caplog.at_level("INFO", logger="polyagent.services.quant.short_horizon.decider"):
            d.evaluate(self._row_with_slug("btc-updown-5m-A"))
        msg = next(r.message for r in caplog.records if "SKIP" in r.message)
        assert "SKIP btc-updown-5m-A" in msg
        assert "reason=open_cap" in msg
        assert "open=3" in msg
        assert "limit=3" in msg

    def test_cycle_cap_logged(self, caplog):
        repo = _FakeRepo()
        d = self._decider(repo, max_per_cycle=0)
        with caplog.at_level("INFO", logger="polyagent.services.quant.short_horizon.decider"):
            d.evaluate(self._row_with_slug("btc-updown-5m-B"))
        msg = next(r.message for r in caplog.records if "SKIP" in r.message)
        assert "reason=cycle_cap" in msg
        assert "limit=0" in msg

    def test_no_spot_logged(self, caplog):
        repo = _FakeRepo()
        d = self._decider(repo, sources={"BTC": _FakeSrc(None)})
        with caplog.at_level("INFO", logger="polyagent.services.quant.short_horizon.decider"):
            d.evaluate(self._row_with_slug("btc-updown-5m-C"))
        msg = next(r.message for r in caplog.records if "SKIP" in r.message)
        assert "reason=no_spot" in msg
        assert "asset=BTC" in msg

    def test_window_closed_logged(self, caplog):
        repo = _FakeRepo()
        d = self._decider(repo)
        row = self._row_with_slug("btc-updown-5m-D")
        row["window_end_ts"] = datetime.now(timezone.utc) - timedelta(seconds=10)
        with caplog.at_level("INFO", logger="polyagent.services.quant.short_horizon.decider"):
            d.evaluate(row)
        msg = next(r.message for r in caplog.records if "SKIP" in r.message)
        assert "reason=window_closed" in msg

    def test_window_not_open_logged_and_skipped(self, caplog):
        """Polymarket lists short-horizon markets hours before they open.
        Entering on those holds a slot for the full listing-to-resolution
        span (often 9+ hours), which is what was starving the cap."""
        repo = _FakeRepo()
        d = self._decider(repo)
        row = self._row_with_slug("btc-updown-5m-future")
        # Window opens in 9 hours, closes 9h5m from now.
        row["window_start_ts"] = datetime.now(timezone.utc) + timedelta(hours=9)
        row["window_end_ts"] = datetime.now(timezone.utc) + timedelta(hours=9, minutes=5)
        with caplog.at_level("INFO", logger="polyagent.services.quant.short_horizon.decider"):
            d.evaluate(row)
        # Skipped before the trade is inserted.
        assert repo.inserted == []
        msg = next(r.message for r in caplog.records if "SKIP" in r.message)
        assert "reason=window_not_open" in msg
        assert "minutes_until_open=" in msg

    def test_window_actively_open_proceeds_past_check(self, caplog):
        """Sanity: if the window is currently open, this gate doesn't fire."""
        repo = _FakeRepo()
        # Edge will clear (high p_up vs low mid).
        sources = {"BTC": _FakeSrc(Decimal("60000"))}
        book = _FakeBook((Decimal("0.30"), Decimal("0.32")))
        d = self._decider(repo, sources=sources, book=book)
        row = self._row_with_slug("btc-updown-5m-now")
        row["window_start_ts"] = datetime.now(timezone.utc) - timedelta(minutes=2)
        row["window_end_ts"] = datetime.now(timezone.utc) + timedelta(minutes=3)
        with caplog.at_level("INFO", logger="polyagent.services.quant.short_horizon.decider"):
            d.evaluate(row)
        # Trade went through (no window_not_open SKIP).
        assert len(repo.inserted) == 1
        assert not any("window_not_open" in r.message for r in caplog.records)

    def test_no_book_logged(self, caplog):
        class _NoBook:
            def fetch_mid(self, _): return None
        repo = _FakeRepo()
        d = self._decider(repo, book=_NoBook())
        with caplog.at_level("INFO", logger="polyagent.services.quant.short_horizon.decider"):
            d.evaluate(self._row_with_slug("btc-updown-5m-E"))
        msg = next(r.message for r in caplog.records if "SKIP" in r.message)
        assert "reason=no_book" in msg
        assert "token=yes_id" in msg

    def test_edge_below_threshold_logged_with_diagnostics(self, caplog):
        repo = _FakeRepo()
        # Mid ~0.50 against spot==start_spot → p_up≈0.5 → edge≈0
        sources = {"BTC": _FakeSrc(Decimal("60000"))}
        book = _FakeBook((Decimal("0.49"), Decimal("0.51")))
        d = self._decider(repo, sources=sources, book=book)
        row = self._row_with_slug("btc-updown-5m-F")
        row["start_spot"] = Decimal("60000")  # match spot exactly
        with caplog.at_level("INFO", logger="polyagent.services.quant.short_horizon.decider"):
            d.evaluate(row)
        msg = next(r.message for r in caplog.records if "SKIP" in r.message)
        assert "reason=edge_below_threshold" in msg
        assert "threshold=0.0500" in msg
        assert "p_up=" in msg
        assert "mid=" in msg

    def test_skip_log_format_is_grep_friendly(self, caplog):
        """Lock the leading-prefix shape: `SKIP <slug> reason=<code>`."""
        repo = _FakeRepo(open_per_asset={"BTC": 3})
        d = self._decider(repo, max_open_per_asset=3)
        with caplog.at_level("INFO", logger="polyagent.services.quant.short_horizon.decider"):
            d.evaluate(self._row_with_slug("btc-updown-5m-Z"))
        # First two tokens must be "SKIP" then the slug.
        msg = next(r.message for r in caplog.records if "SKIP" in r.message)
        tokens = msg.split()
        assert tokens[0] == "SKIP"
        assert tokens[1] == "btc-updown-5m-Z"
        assert tokens[2].startswith("reason=")


class TestBankrollIntegration:
    """Bankroll provider gates entries below floor and Kelly-sizes above it."""

    def _decider_with_bankroll(self, repo, free, *, kelly_max_fraction=0.25,
                               max_size="5", min_floor="1.0"):
        # provider closure returns whatever BankrollState we wire in
        bk = BankrollState(
            starting=Decimal("20"),
            realized_main=Decimal("0"),
            realized_quant=Decimal("0"),
            open_capital_main=Decimal("0"),
            open_capital_quant=Decimal("20") - Decimal(str(free)),
        )
        return QuantDecider(
            sources={"BTC": _FakeSrc(Decimal("60000"))},
            book=_FakeBook((Decimal("0.30"), Decimal("0.32"))),
            repo=repo,
            position_size_usd=Decimal(str(max_size)),
            settlements={"BTC": _FakeSettlement(Decimal("59000"))},
            bankroll_provider=lambda: bk,
            kelly_max_fraction=kelly_max_fraction,
            min_free_bankroll=Decimal(str(min_floor)),
        )

    def test_floor_skip_when_free_below_min(self, caplog):
        repo = _FakeRepo()
        d = self._decider_with_bankroll(repo, free="0.50", min_floor="1.0")
        row = _row()
        row["slug"] = "btc-updown-5m-floor"
        with caplog.at_level("INFO", logger="polyagent.services.quant.short_horizon.decider"):
            d.evaluate(row)
        assert repo.inserted == []
        msg = next(r.message for r in caplog.records if "SKIP" in r.message and "bankroll_floor" in r.message)
        assert "reason=bankroll_floor" in msg
        assert "free=0.50" in msg
        assert "floor=1.00" in msg

    def test_kelly_sizes_below_max_when_bankroll_tight(self):
        # spot=60000 vs start_spot=59000: p_up ≈ 1.0, mid = 0.31, so
        # edge ≈ 0.69. raw_kelly = 0.69 × 0.25 × 4.00 = 0.69. Cap=5,
        # headroom = 4 - 1 = 3. min(0.69, 5, 3) = 0.69.
        # Integer-contract floor at fill=0.32: floor(0.69/0.32)=2 contracts
        # → 2 × 0.32 = 0.64.
        repo = _FakeRepo()
        d = self._decider_with_bankroll(repo, free="4.00", max_size="5",
                                        kelly_max_fraction=0.25)
        d.evaluate(_row())
        assert len(repo.inserted) == 1
        assert repo.inserted[0].size == Decimal("0.64")

    def test_kelly_size_caps_at_position_size_when_bankroll_large(self):
        repo = _FakeRepo()
        d = self._decider_with_bankroll(repo, free="200", max_size="5",
                                        kelly_max_fraction=0.25)
        d.evaluate(_row())
        assert len(repo.inserted) == 1
        # raw = |edge| × 0.25 × 200 = 50; capped at 5. Integer contracts
        # at fill=0.32: floor(5/0.32)=15 contracts → 15 × 0.32 = 4.80.
        assert repo.inserted[0].size == Decimal("4.80")

    def test_kelly_respects_headroom(self):
        # Free = $1.50, floor = $1.00. Headroom = 0.50. With a max-Kelly
        # setting of 1.0 the raw Kelly = 0.69 × 1.0 × 1.50 = 1.035, which
        # exceeds both the per-trade cap (5) and the headroom (0.50). The
        # min is the headroom. Integer contracts at fill=0.32:
        # floor(0.50/0.32)=1 contract → 1 × 0.32 = 0.32.
        repo = _FakeRepo()
        d = self._decider_with_bankroll(repo, free="1.50", max_size="5",
                                        min_floor="1.0", kelly_max_fraction=1.0)
        d.evaluate(_row())
        assert len(repo.inserted) == 1
        assert repo.inserted[0].size == Decimal("0.32")

    def test_no_bankroll_provider_keeps_legacy_fixed_size(self):
        """Backward-compat: tests and non-wired call sites size off the
        fixed position_size_usd, no Kelly scaling, no floor — but still
        flooring to whole contracts (Polymarket only fills integer lots).
        At fill=0.32: floor(5/0.32)=15 contracts → 15 × 0.32 = 4.80."""
        repo = _FakeRepo()
        d = QuantDecider(
            sources={"BTC": _FakeSrc(Decimal("60000"))},
            book=_FakeBook((Decimal("0.30"), Decimal("0.32"))),
            repo=repo,
            position_size_usd=Decimal("5"),
        )
        d.evaluate(_row())
        assert len(repo.inserted) == 1
        assert repo.inserted[0].size == Decimal("4.80")
