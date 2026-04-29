"""Re-export shim. Real implementation lives in
polyagent.services.quant.assets.sources.coinbase.
"""
from polyagent.services.quant.assets.sources.coinbase import CoinbaseSpotSource

# Back-compat alias used by the old btc5m subsystem.
BtcSpotSource = CoinbaseSpotSource

__all__ = ["CoinbaseSpotSource", "BtcSpotSource"]
