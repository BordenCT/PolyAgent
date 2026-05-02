"""PolyAgent entry point — boots the worker pool and runs the pipeline."""
from __future__ import annotations

import logging
import os
import signal
import time
from datetime import datetime, timezone
from decimal import Decimal
from queue import Empty

from polyagent.data.clients.claude import ClaudeClient
from polyagent.data.clients.ollama import OllamaClient
from polyagent.data.clients.polymarket import PolymarketClient
from polyagent.data.repositories.historical import HistoricalRepository
from polyagent.data.repositories.markets import MarketRepository
from polyagent.data.repositories.positions import PositionRepository
from polyagent.data.repositories.thesis import ThesisRepository
from polyagent.data.repositories.trade_log import TradeLogRepository
from polyagent.infra.config import Settings
from polyagent.infra.database import Database
from polyagent.infra.logging import setup_logging
from polyagent.infra.pool import WorkerPool
from polyagent.infra.queues import Queues, ScanResult, ThesisResult
from polyagent.models import MarketStatus
from polyagent.services.bankroll import compute_bankroll_state
from polyagent.services.brain import BrainService
from polyagent.services.classifier import classify
from polyagent.services.embeddings import EmbeddingsService
from polyagent.services.executor import ExecutorService
from polyagent.services.exit_monitor import ExitMonitorService
from polyagent.services.quant.assets.registry import apply_env_overrides, enabled_for
from polyagent.services.quant.assets.spec import MarketFamily
from polyagent.services.quant.assets.sources.coinbase import CoinbaseSpotSource
from polyagent.services.quant.orchestrator import run_quant_orchestrator
from polyagent.services.quant.short_horizon.book import PolymarketBookFetcher
from polyagent.services.quant.short_horizon.decider import QuantDecider
from polyagent.services.quant.short_horizon.repository import QuantShortRepository
from polyagent.services.quant.short_horizon.resolver import QuantResolver
from polyagent.services.quant.short_horizon.scanner import QuantShortScanner
from polyagent.services.quant.strike import QuantStrikeService
from polyagent.services.scanner import ScannerService
from polyagent.strategies.arbitrage import ArbitrageStrategy
from polyagent.strategies.convergence import ConvergenceStrategy
from polyagent.strategies.whale_copy import WhaleCopyStrategy

logger = logging.getLogger("polyagent.main")


def run() -> None:
    """Main entry point for the bot."""
    setup_logging(level=os.environ.get("LOG_LEVEL", "INFO"))
    settings = Settings.from_env()
    live_enabled = not settings.paper_trade and settings.polymarket_live_enabled
    logger.info(
        "PolyAgent starting (paper_trade=%s, live_enabled=%s)",
        settings.paper_trade, live_enabled,
    )

    db = Database(settings)
    queues = Queues()
    pool = WorkerPool()

    polymarket = PolymarketClient(base_url=settings.polymarket_api_url)
    embeddings = EmbeddingsService(api_key=settings.voyage_api_key)

    ollama = None
    claude = None
    if settings.llm_provider in ("ollama", "hybrid"):
        ollama = OllamaClient(base_url=settings.ollama_url, model=settings.ollama_model)
        if ollama.health_check():
            logger.info("Ollama connected: %s @ %s", settings.ollama_model, settings.ollama_url)
        else:
            logger.warning("Ollama unreachable at %s", settings.ollama_url)
            ollama = None

    if settings.llm_provider in ("claude", "hybrid") or (settings.llm_provider == "ollama" and not ollama):
        if settings.anthropic_api_key:
            claude = ClaudeClient(api_key=settings.anthropic_api_key)
            logger.info("Claude client initialized")
        elif settings.llm_provider == "claude":
            raise ValueError("LLM_PROVIDER=claude but ANTHROPIC_API_KEY is not set")

    if settings.llm_provider == "ollama" and ollama:
        brain_evaluator = ollama
        logger.info("Brain using: Ollama phi4:14b ($0)")
    elif settings.llm_provider == "hybrid" and claude:
        brain_evaluator = claude
        logger.info("Brain using: Claude Sonnet (scanner uses Ollama)")
    elif claude:
        brain_evaluator = claude
        logger.info("Brain using: Claude Sonnet")
    else:
        raise RuntimeError("No LLM available for brain evaluation")

    market_repo = MarketRepository(db)
    thesis_repo = ThesisRepository(db)
    position_repo = PositionRepository(db)
    historical_repo = HistoricalRepository(db)
    trade_log_repo = TradeLogRepository(db)
    quant_short_repo = QuantShortRepository(db)

    scanner = ScannerService(
        min_gap=settings.min_gap,
        min_depth=settings.min_depth,
        min_hours=settings.min_hours,
        max_hours=settings.max_hours,
        min_price=settings.min_price,
        max_price=settings.max_price,
        question_blocklist=settings.scanner_question_blocklist,
    )
    # Build the unified PriceSource dict consumed by the strike service AND
    # the short-horizon orchestrator. One Coinbase ticker per asset per
    # process; no double-polling.
    quant_specs_short = [apply_env_overrides(s) for s in enabled_for(MarketFamily.SHORT_HORIZON)]
    quant_specs_strike = [apply_env_overrides(s) for s in enabled_for(MarketFamily.STRIKE)]
    asset_ids: set[str] = (
        {s.asset_id for s in quant_specs_short} | {s.asset_id for s in quant_specs_strike}
    )
    quant_sources: dict[str, CoinbaseSpotSource] = {}
    quant_settlements: dict[str, CoinbaseSpotSource] = {}
    for asset_id in sorted(asset_ids):
        # Both the price source and the settlement source come from the
        # registry's per-asset factories so a future asset can swap to e.g.
        # ChainlinkSpotSource without touching this file.
        spec = next(
            (s for s in quant_specs_strike + quant_specs_short if s.asset_id == asset_id),
            None,
        )
        if spec is None:
            continue
        quant_sources[asset_id] = spec.price_source()
        quant_settlements[asset_id] = spec.settlement_source()
        # Prime the spot cache so the first scan after startup has data.
        quant_sources[asset_id].tick()

    crypto_quant: QuantStrikeService | None = None
    if settings.quant_strike_enabled and quant_sources:
        crypto_quant = QuantStrikeService(sources=quant_sources)
        logger.info(
            "quant strike enabled: assets=%s",
            sorted(quant_sources.keys()),
        )

    brain = BrainService(
        llm_evaluator=brain_evaluator,
        embeddings_service=embeddings,
        historical_repo=historical_repo,
        confidence_threshold=settings.brain_confidence_threshold,
        min_checks=settings.brain_min_checks,
        min_edge=settings.brain_min_edge,
        crypto_quant=crypto_quant,
    )
    executor = ExecutorService(
        kelly_max_fraction=settings.kelly_max_fraction,
        bankroll=settings.bankroll,
        paper_trade=settings.paper_trade,
        min_free_bankroll=settings.min_free_bankroll,
        min_order_size=settings.min_order_size,
    )
    exit_monitor = ExitMonitorService(
        target_pct=settings.exit_target_pct,
        volume_multiplier=settings.exit_volume_multiplier,
        stale_hours=settings.exit_stale_hours,
        stale_threshold=settings.exit_stale_threshold,
    )

    strategies = [ArbitrageStrategy(), ConvergenceStrategy(), WhaleCopyStrategy()]

    def scanner_worker():
        """Fetch markets, score, push survivors to scan_queue."""
        while queues.shutdown.empty():
            try:
                raw_markets = polymarket.fetch_markets(limit=settings.scan_market_limit)
                markets = []
                for raw in raw_markets:
                    parsed = polymarket.parse_market(raw)
                    if parsed:
                        markets.append(parsed)

                if ollama:
                    questions = [{"id": m.polymarket_id, "question": m.question} for m in markets]
                    estimates = ollama.estimate_batch(questions)
                else:
                    estimates = {}  # scanner falls back to 0.5 neutral prior per market
                survivors = scanner.scan_batch(markets, estimates)

                open_market_ids = position_repo.get_open_market_ids()
                cooldown_market_ids = position_repo.get_recently_closed_market_ids(
                    settings.market_cooldown_hours
                )
                skipped_open = 0
                skipped_cooldown = 0
                for market, score in survivors:
                    market.market_class = classify(market.question, market.category)
                    db_id = market_repo.upsert(market, MarketStatus.QUEUED)
                    if db_id in open_market_ids:
                        skipped_open += 1
                        continue
                    if db_id in cooldown_market_ids:
                        skipped_cooldown += 1
                        continue
                    market_repo.update_score(db_id, score, MarketStatus.QUEUED)
                    queues.scan_queue.put(ScanResult(market=market, market_db_id=db_id, score=score))
                if skipped_open or skipped_cooldown:
                    logger.info(
                        "Skipped %d markets with open positions, %d in %.0fh cooldown",
                        skipped_open, skipped_cooldown, settings.market_cooldown_hours,
                    )

                logger.info("Scan cycle complete. Sleeping %dh", settings.scan_interval_hours)
                time.sleep(settings.scan_interval_hours * 3600)
            except Exception:
                logger.exception("Scanner error")
                time.sleep(60)

    def brain_worker():
        """Pull from scan_queue, evaluate, push to thesis_queue."""
        while queues.shutdown.empty():
            try:
                scan_result = queues.scan_queue.get(timeout=30)
                # Back-to-back scan cycles can queue the same market multiple
                # times. Re-check at dequeue to avoid evaluating a market that
                # another worker already opened a position on.
                if scan_result.market_db_id in position_repo.get_open_market_ids():
                    market_repo.update_status(scan_result.market_db_id, MarketStatus.TRADED)
                    queues.scan_queue.task_done()
                    continue
                thesis = brain.evaluate(scan_result.market, scan_result.market_db_id)
                if thesis:
                    thesis_repo.insert(thesis)
                    queues.thesis_queue.put(ThesisResult(thesis=thesis, market=scan_result.market))
                else:
                    market_repo.update_status(scan_result.market_db_id, MarketStatus.REJECTED)
                queues.scan_queue.task_done()
            except Empty:
                continue
            except Exception:
                logger.exception("Brain error")

    def executor_worker():
        """Pull from thesis_queue, run consensus, execute trades."""
        while queues.shutdown.empty():
            try:
                item = queues.thesis_queue.get(timeout=30)
                thesis, market = item.thesis, item.market

                # Final dedup guard — a position may have been opened on this
                # market between the brain's check and now.
                if thesis.market_id in position_repo.get_open_market_ids():
                    logger.info(
                        "SKIP %s — already holding an open position on this market",
                        thesis.market_id,
                    )
                    market_repo.update_status(thesis.market_id, MarketStatus.TRADED)
                    queues.thesis_queue.task_done()
                    continue

                votes = []
                for strategy in strategies:
                    if hasattr(strategy, "name") and strategy.name == "whale_copy":
                        vote = strategy.evaluate(whale_positions=[], min_whale_count=2)
                    elif hasattr(strategy, "name") and strategy.name == "convergence":
                        vote = strategy.evaluate(
                            claude_estimate=thesis.claude_estimate,
                            market_price=float(market.midpoint_price),
                            price_history=[],
                        )
                    else:
                        vote = strategy.evaluate(
                            claude_estimate=thesis.claude_estimate,
                            market_price=float(market.midpoint_price),
                            related_markets=[],
                        )
                    votes.append(vote)

                # Unified across `positions` (main) and `quant_short_trades`
                # (short-horizon paper). Both subsystems draw from the same
                # paper bankroll, so the floor and Kelly sizing should see
                # the same number.
                bk_state = compute_bankroll_state(db, settings.bankroll)
                open_capital = bk_state.open_capital_total
                realized_pnl = bk_state.realized_total
                free_bankroll = float(bk_state.free)
                if free_bankroll < settings.min_free_bankroll:
                    logger.info(
                        "SKIP %s — free bankroll $%.2f below $%.2f floor (open=$%.2f, pnl=$%.2f)",
                        thesis.market_id,
                        free_bankroll,
                        settings.min_free_bankroll,
                        float(open_capital),
                        float(realized_pnl),
                    )
                    market_repo.update_status(thesis.market_id, MarketStatus.REJECTED)
                    queues.thesis_queue.task_done()
                    continue

                if live_enabled:
                    position = executor.execute_live(
                        thesis=thesis,
                        votes=votes,
                        market=market,
                        polymarket_client=polymarket,
                        trade_log=trade_log_repo,
                        current_bankroll=free_bankroll,
                    )
                else:
                    position = executor.execute(
                        thesis=thesis,
                        votes=votes,
                        market_price=market.midpoint_price,
                        volume_at_entry=market.volume_24h,
                        current_bankroll=free_bankroll,
                    )

                if position:
                    position_repo.insert(position)
                    market_repo.update_status(thesis.market_id, MarketStatus.TRADED)
                    if not live_enabled:
                        trade_log_repo.insert(
                            position_id=position.id,
                            action="OPEN_PAPER",
                            reason=thesis.consensus.value,
                            raw_request={
                                "token_id": market.token_id,
                                "side": position.side.value,
                                "price": float(position.entry_price),
                                "size": float(position.position_size),
                            },
                        )

                thesis_repo.update_votes(
                    thesis.id,
                    thesis.strategy_votes,
                    thesis.consensus,
                )
                queues.thesis_queue.task_done()
            except Empty:
                continue
            except Exception:
                logger.exception("Executor error")

    def exit_monitor_worker():
        """Poll open positions, refresh state, fire exit triggers."""
        while queues.shutdown.empty():
            try:
                open_positions = position_repo.get_open()
                for pos in open_positions:
                    if not queues.shutdown.empty():
                        break
                    time.sleep(settings.exit_poll_delay)
                    snapshot = polymarket.fetch_market_state(pos["polymarket_id"])
                    current_price = snapshot["midpoint_price"] if snapshot else pos["current_price"]
                    current_volume = float(snapshot["volume_24h"]) if snapshot else float(pos.get("volume_at_entry") or 0)
                    is_resolved = bool(snapshot["is_resolved"]) if snapshot else False

                    # Don't persist zero-midpoint readings unless the market is
                    # actually resolved — thin books produce spurious zeros.
                    zero_like = float(current_price) <= exit_monitor._resolved_no_threshold
                    if snapshot and current_price != pos["current_price"] and (is_resolved or not zero_like):
                        position_repo.update_price(pos["id"], current_price)

                    hours_since_entry = _hours_since(pos["opened_at"])
                    volume_at_entry = float(pos.get("volume_at_entry") or 0)
                    avg_rate = volume_at_entry / 144.0
                    current_rate = current_volume / 144.0

                    reason = exit_monitor.check_exit(
                        entry_price=pos["entry_price"],
                        target_price=pos["target_price"],
                        current_price=current_price,
                        volume_10min=current_rate,
                        avg_volume_10min=avg_rate,
                        hours_since_entry=hours_since_entry,
                        is_resolved=is_resolved,
                    )
                    if reason:
                        pnl = exit_monitor.calculate_pnl(
                            entry_price=pos["entry_price"],
                            exit_price=current_price,
                            position_size=pos["position_size"],
                            side=pos["side"],
                        )
                        position_repo.close(pos["id"], reason, pnl, current_price)
                        trade_log_repo.insert(
                            position_id=pos["id"],
                            action="CLOSE_PAPER" if pos.get("paper_trade", True) else "CLOSE_LIVE",
                            reason=reason.value,
                            raw_request={
                                "entry_price": float(pos["entry_price"]),
                                "exit_price": float(current_price),
                                "position_size": float(pos["position_size"]),
                                "hours_since_entry": hours_since_entry,
                            },
                        )
                        logger.info("CLOSED %s — %s pnl=$%.2f", pos["id"], reason.value, pnl)

                time.sleep(60)
            except Exception:
                logger.exception("Exit monitor error")
                time.sleep(60)

    n_scanner = pool.compute_workers("scanner", 3, settings.scanner_workers)
    n_brain = pool.compute_workers("brain", 6, settings.brain_workers)
    n_executor = pool.compute_workers("executor", 24, settings.executor_workers)
    n_exit = settings.exit_workers if settings.exit_workers is not None else 1
    logger.info("exit_monitor: %d workers (pinned low to respect CLOB rate limit)", n_exit)

    pool.spawn("scanner", scanner_worker, n_scanner)
    pool.spawn("brain", brain_worker, n_brain)
    pool.spawn("executor", executor_worker, n_executor)
    pool.spawn("exit_monitor", exit_monitor_worker, n_exit)

    # Single quant orchestrator owns all PriceSource ticks plus the
    # short-horizon scan/decide/resolve loop. The strike service shares the
    # same source dict, so there's no double-polling.
    if quant_sources:
        book_fetcher = PolymarketBookFetcher(polymarket)
        quant_short_scanner = QuantShortScanner()
        quant_decider = QuantDecider(
            sources=quant_sources,
            book=book_fetcher,
            repo=quant_short_repo,
            position_size_usd=Decimal(str(settings.quant_position_size_usd)),
            max_trades_per_cycle=settings.quant_max_trades_per_cycle,
            max_open_per_asset=settings.quant_max_open_per_asset,
            settlements=quant_settlements,
            # Both bots draw from the same paper bankroll. Decider gates
            # against state.free below min_free_bankroll, and Kelly-sizes
            # each entry from state.free so total exposure compounds down
            # naturally as positions stack up.
            bankroll_provider=lambda: compute_bankroll_state(db, settings.bankroll),
            kelly_max_fraction=settings.kelly_max_fraction,
            min_free_bankroll=Decimal(str(settings.min_free_bankroll)),
            min_order_size=Decimal(str(settings.min_order_size)),
        )
        quant_resolver = QuantResolver(
            repo=quant_short_repo,
            client=polymarket,
            settlements=quant_settlements,
        )

        def quant_scan_and_decide() -> None:
            if not settings.quant_short_enabled:
                return
            cycle_start = time.time()
            logger.info("quant cycle: start")
            scanned = 0
            try:
                for m in quant_short_scanner.scan():
                    quant_short_repo.upsert_market(m)
                    scanned += 1
            except Exception:
                logger.exception("quant scan failed")
            logger.info("quant cycle: scan done scanned=%d", scanned)
            active_count = 0
            try:
                quant_decider.reset_cycle()
                active = quant_short_repo.get_active_markets(datetime.now(timezone.utc))
                active_count = len(active)
                for row in active:
                    quant_decider.evaluate(row)
            except Exception:
                logger.exception("quant decider failed")
            logger.info(
                "quant cycle: decide done active=%d entered=%d",
                active_count, quant_decider.opened_this_cycle,
            )
            resolved = 0
            try:
                resolved = quant_resolver.resolve_due_markets() or 0
            except Exception:
                logger.exception("quant resolver failed")
            logger.info(
                "quant cycle: complete resolved=%d elapsed_s=%.2f",
                resolved, time.time() - cycle_start,
            )

        pool.spawn(
            "quant_orchestrator",
            lambda: run_quant_orchestrator(
                sources=quant_sources,
                specs=quant_specs_short + quant_specs_strike,
                scan_and_decide=quant_scan_and_decide,
                market_interval_s=settings.quant_market_poll_s,
                shutdown_q=queues.shutdown,
            ),
            1,
        )
        logger.info(
            "quant_orchestrator: 1 worker enabled (short=%s strike=%s assets=%s)",
            settings.quant_short_enabled,
            settings.quant_strike_enabled,
            sorted(quant_sources.keys()),
        )
    else:
        logger.info("quant: disabled (no assets enabled in registry)")

    logger.info(
        "All workers started: %d scanner, %d brain, %d executor, %d exit",
        n_scanner, n_brain, n_executor, n_exit,
    )

    def shutdown_handler(signum, frame):
        logger.info("Shutdown signal received")
        queues.shutdown.put(True)

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    try:
        while queues.shutdown.empty():
            time.sleep(1)
    finally:
        logger.info("Shutting down...")
        polymarket.close()
        if claude:
            claude.close()
        if ollama:
            ollama.close()
        db.close()
        pool.join_all(timeout=10)
        logger.info("PolyAgent stopped")


def _hours_since(ts) -> float:
    """Hours elapsed between ts (timezone-aware datetime) and now."""
    if ts is None:
        return 0.0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0


if __name__ == "__main__":
    run()
