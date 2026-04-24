"""Decision engine + paper-trade executor for BTC 5m markets."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from polyagent.data.repositories.btc5m import Btc5mRepository
from polyagent.models import Btc5mTrade
from polyagent.services.btc5m.estimator import estimate_up_probability
from polyagent.services.btc5m.spot import BtcSpotSource

logger = logging.getLogger("polyagent.services.btc5m.decider")


class BookFetcher:
    """Lightweight protocol for orderbook fetches.

    Concrete implementations (e.g. PolymarketBookFetcher) are provided by
    Task 10. Tests and callers can supply any object with a matching
    ``fetch_mid`` method without subclassing.
    """

    def fetch_mid(self, token_id: str) -> tuple[Decimal, Decimal] | None:
        """Return (best_bid, best_ask) for the YES token, or None on failure.

        Args:
            token_id: CLOB token ID for the YES side.

        Returns:
            A (best_bid, best_ask) tuple, or None if the book is unavailable.
        """
        raise NotImplementedError


class Btc5mDecider:
    """For each active market: compute edge, paper-trade if it clears threshold.

    Args:
        spot: Live BTC/USD spot source with realized-vol.
        book: Orderbook mid fetcher (duck-typed; need not subclass BookFetcher).
        repo: Btc5m repository for idempotency checks and trade insertion.
        edge_threshold: Minimum |estimator_p_up - market_mid| to enter.
        position_size_usd: Notional USD of each paper trade.
        fees_bps: Round-trip fee in basis points; trade rejected if fees >= gross_edge.
        vol_window_s: Lookback in seconds for realized-vol computation.
    """

    def __init__(
        self,
        spot: BtcSpotSource,
        book: BookFetcher,
        repo: Btc5mRepository,
        edge_threshold: float,
        position_size_usd: Decimal,
        fees_bps: float,
        vol_window_s: int = 300,
    ) -> None:
        self._spot = spot
        self._book = book
        self._repo = repo
        self._threshold = edge_threshold
        self._size = position_size_usd
        self._fees_bps = fees_bps
        self._vol_window_s = vol_window_s

    def evaluate(self, market_row: dict) -> None:
        """Evaluate one active market and record a paper trade if edge clears.

        Guards (returns without trading) if:
        - The market already has a trade recorded (one trade per market).
        - The spot source has no price data.
        - TTM is <= 0 (window already closed; resolver handles these).
        - The book fetch fails.
        - |edge| < edge_threshold.
        - Gross edge in USD does not cover fees.

        Uses worst-case fill prices: ask for YES entries, bid for NO entries.

        Args:
            market_row: A btc5m_markets row dict as returned by
                        ``Btc5mRepository.get_active_markets``.
        """
        market_id = market_row["id"]
        if self._repo.get_trades_for_market(market_id):
            return  # one trade per market

        spot = self._spot.current()
        if spot is None:
            return

        vol = self._spot.realized_vol(window_s=self._vol_window_s)

        window_end = market_row["window_end_ts"]
        now = datetime.now(timezone.utc)
        ttm = (window_end - now).total_seconds()
        if ttm <= 0:
            return  # resolver will handle

        # Before the window opens, start_spot is unknown — proxy with the
        # current spot so the estimator treats it as an ATM position.
        # Once the window is open, start_spot holds the recorded opening price.
        start_spot = market_row.get("start_spot")
        if start_spot is None:
            start_spot = spot

        p_up = estimate_up_probability(
            start_price=start_spot,
            current_spot=spot,
            seconds_to_resolution=ttm,
            annualised_vol=vol,
        )

        book = self._book.fetch_mid(market_row["token_id_yes"])
        if book is None:
            return
        bid, ask = book
        mid = (float(bid) + float(ask)) / 2.0

        edge = p_up - mid
        if abs(edge) < self._threshold:
            return

        size_fraction = float(self._size)
        gross_edge_usd = abs(edge) * size_fraction
        fees_usd = size_fraction * self._fees_bps / 10_000.0
        if gross_edge_usd <= fees_usd:
            return

        if edge > 0:
            side = "YES"
            fill = ask
        else:
            side = "NO"
            fill = bid

        trade = Btc5mTrade(
            market_id=market_id,
            side=side,
            fill_price_assumed=fill,
            size=self._size,
            estimator_p_up=p_up,
            spot_at_decision=spot,
            vol_at_decision=vol,
            edge_at_decision=edge,
        )
        self._repo.insert_trade(trade)
        logger.info(
            "PAPER TRADE %s on %s: side=%s edge=%+.3f p_up=%.3f mid=%.3f",
            trade.side, market_row["polymarket_id"], side, edge, p_up, mid,
        )
