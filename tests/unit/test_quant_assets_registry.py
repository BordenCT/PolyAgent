from polyagent.services.quant.assets.registry import (
    ASSETS, get, enabled_for, live_eligible, apply_env_overrides,
)
from polyagent.services.quant.assets.spec import (
    AssetClass, AssetSpec, MarketFamily,
)


def test_btc_and_eth_registered():
    assert "BTC" in ASSETS
    assert "ETH" in ASSETS
    assert ASSETS["BTC"].asset_class == AssetClass.CRYPTO
    assert ASSETS["ETH"].asset_class == AssetClass.CRYPTO


def test_get_returns_spec_or_none():
    assert get("BTC").asset_id == "BTC"
    assert get("XAU") is None


def test_enabled_for_filters_by_market_family():
    short = enabled_for(MarketFamily.SHORT_HORIZON)
    strike = enabled_for(MarketFamily.STRIKE)
    short_ids = [s.asset_id for s in short]
    strike_ids = [s.asset_id for s in strike]
    assert "BTC" in short_ids
    assert "BTC" in strike_ids
    assert "ETH" in strike_ids


def test_live_eligible_excludes_paper_only(monkeypatch):
    base = ASSETS["BTC"]
    paper_btc = AssetSpec(
        asset_id="BTC",
        asset_class=AssetClass.CRYPTO,
        price_source=base.price_source,
        settlement_source=base.settlement_source,
        default_vol=0.6,
        vol_calibration=base.vol_calibration,
        supported_market_families=frozenset({MarketFamily.SHORT_HORIZON}),
        paper_only=True,
    )
    monkeypatch.setitem(ASSETS, "BTC", paper_btc)
    eligible = live_eligible(MarketFamily.SHORT_HORIZON)
    assert all(s.asset_id != "BTC" for s in eligible)


def test_apply_env_overrides_replaces_default_vol(monkeypatch):
    monkeypatch.setenv("QUANT_BTC_VOL", "0.85")
    spec = apply_env_overrides(ASSETS["BTC"])
    assert spec.default_vol == 0.85


def test_apply_env_overrides_replaces_edge_threshold(monkeypatch):
    monkeypatch.setenv("QUANT_BTC_EDGE_THRESHOLD", "0.10")
    spec = apply_env_overrides(ASSETS["BTC"])
    assert spec.edge_threshold == 0.10


def test_apply_env_overrides_no_env_returns_unchanged():
    original = ASSETS["BTC"]
    spec = apply_env_overrides(original)
    assert spec.default_vol == original.default_vol
    assert spec.edge_threshold == original.edge_threshold
