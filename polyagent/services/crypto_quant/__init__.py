"""Quantitative crypto-strike estimator.

Replaces the LLM brain on Polymarket questions of the form

    "Will the price of {Bitcoin|Ethereum} be {above|below|between} $K[..$K2]
     on {date}?"

The LLM has no live spot reference and consistently mis-prices these. Closed-
form Black-Scholes Φ(d₂) using a Coinbase spot feed gives a deterministic,
calibrated probability. Range markets reduce to P(above K_low) − P(above K_high).
Barrier-touch markets ("dip to" / "reach") are deferred to a later iteration.
"""
from polyagent.services.crypto_quant.estimator import (
    QuantResult,
    estimate_yes_probability,
)
from polyagent.services.crypto_quant.parser import (
    CryptoStrike,
    StrikeKind,
    parse_question,
)
from polyagent.services.crypto_quant.service import CryptoQuantService

__all__ = [
    "CryptoQuantService",
    "CryptoStrike",
    "QuantResult",
    "StrikeKind",
    "estimate_yes_probability",
    "parse_question",
]
