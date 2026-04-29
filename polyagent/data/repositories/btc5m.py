"""Re-export shim. Real implementation lives in
polyagent.services.quant.short_horizon.repository. Deleted in PR 6.
"""
from polyagent.services.quant.short_horizon.repository import QuantShortRepository

# Back-compat alias used by btc5m worker until PR 6.
Btc5mRepository = QuantShortRepository

__all__ = ["QuantShortRepository", "Btc5mRepository"]
