"""Re-export shim. Real implementation lives in polyagent.services.quant.strike.

This module will be deleted in PR 6.
"""
from polyagent.services.quant.strike import (
    ParsedStrike, QuantResult, QuantStrikeService, StrikeKind,
)

# Back-compat aliases for legacy callers.
CryptoQuantService = QuantStrikeService
CryptoStrike = ParsedStrike

__all__ = [
    "CryptoQuantService",
    "CryptoStrike",
    "ParsedStrike",
    "QuantResult",
    "QuantStrikeService",
    "StrikeKind",
]
