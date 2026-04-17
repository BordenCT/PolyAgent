"""PolyAgent entry point — boots the worker pool and runs the pipeline."""
from __future__ import annotations

import logging
import signal
import time
from queue import Empty

from polyagent.data.clients.claude import ClaudeClient
from polyagent.data.clients.polymarket import PolymarketClient
from polyagent.data.repositories.historical import HistoricalRepository
from polyagent.data.repositories.markets import MarketRepository
from polyagent.data.repositories.positions import PositionRepository
from polyagent.data.repositories.thesis import ThesisRepository
from polyagent.infra.config import Settings
from polyagent.infra.database import Database
from polyagent.infra.logging import setup_logging
from polyagent.infra.pool import WorkerPool
from polyagent.infra.queues import Queues, ScanResult
from polyagent.models import MarketStatus
from polyagent.services.brain import BrainService
from polyagent.services.embeddings import EmbeddingsService
from polyagent.services.executor import ExecutorService
from polyagent.services.exit_monitor import ExitMonitorService
from polyagent.services.scanner import ScannerService
from polyagent.strategies.arbitrage import ArbitrageStrategy
from polyagent.strategies.convergence import ConvergenceStrategy
from polyagent.data.clients.ollama import OllamaClient
from polyagent.strategies.whale_copy import WhaleCopyStrategy

logger = logging.getLogger("polyagent.main")


def run() -> None:
    """Main entry point for the bot."""
    setup_logging()
    settings = Settings.from_env()
    logger.info("PolyAgent starting (paper_trade=%s)", settings.paper_trade)

    # Infrastructure
    db = Database(settings)
    queues = Queues()
    pool = WorkerPool()

    # Clients
    polymarket = PolymarketClient(base_url=settings.polymarket_api_url)
    embeddings = EmbeddingsService(api_key=settings.voyage_api_key)

    # LLM provider routing
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

    # Select brain evaluator based on provider
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

    # Repositories
    market_repo = MarketRepository(db)
    thesis_repo = ThesisRepository(db)
    position_repo = PositionRepository(db)
    historical_repo = HistoricalRepository(db)

    # Services
    scanner = ScannerService(
        min_gap=settings.min_gap,
        min_depth=settings.min_depth,
        min_hours=settings.min_hours,
        max_hours=settings.max_hours,
    )
    brain = BrainService(
        llm_evaluator=brain_evaluator,
        embeddings_service=embeddings,
        historical_repo=historical_repo,
        confidence_threshold=settings.brain_confidence_threshold,
        min_checks=settings.brain_min_checks,
    )
    executor = ExecutorService(
        kelly_max_fraction=settings.kelly_max_fraction,
        bankroll=settings.bankroll,
        paper_trade=settings.paper_trade,
    )
    exit_monitor = ExitMonitorService(
        target_pct=settings.exit_target_pct,
        volume_multiplier=settings.exit_volume_multiplier,
        stale_hours=settings.exit_stale_hours,
        stale_threshold=settings.exit_stale_threshold,
    )

    strategies = [ArbitrageStrategy(), ConvergenceStrategy(), WhaleCopyStrategy()]

    # --- Worker functions ---

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

                # Get probability estimates: Ollama (free) or midpoint fallback
                if ollama:
                    questions = [{"id": m.polymarket_id, "question": m.question} for m in markets]
                    estimates = ollama.estimate_batch(questions)
                else:
                    estimates = {m.polymarket_id: float(m.midpoint_price) for m in markets}
                survivors = scanner.scan_batch(markets, estimates)

                for market, score in survivors:
                    db_id = market_repo.upsert(market, MarketStatus.QUEUED)
                    market_repo.update_score(db_id, score, MarketStatus.QUEUED)
                    queues.scan_queue.put(ScanResult(market=market, market_db_id=db_id, score=score))

                logger.info("Scan cycle complete. Sleeping %dh", settings.scan_interval_hours)
                time.sleep(settings.scan_interval_hours * 3600)
            except Exception:
                logger.exception("Scanner error")
                time.sleep(60)

    def brain_worker():
        """Pull from scan_queue, evaluate via Claude, push to thesis_queue."""
        while queues.shutdown.empty():
            try:
                scan_result = queues.scan_queue.get(timeout=30)
                thesis = brain.evaluate(scan_result.market, scan_result.market_db_id)
                if thesis:
                    thesis_repo.insert(thesis)
                    queues.thesis_queue.put(thesis)
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
                thesis = queues.thesis_queue.get(timeout=30)

                # Run all strategies
                votes = []
                for strategy in strategies:
                    if hasattr(strategy, "name") and strategy.name == "whale_copy":
                        vote = strategy.evaluate(whale_positions=[], min_whale_count=2)
                    elif hasattr(strategy, "name") and strategy.name == "convergence":
                        vote = strategy.evaluate(
                            claude_estimate=thesis.claude_estimate,
                            market_price=float(thesis.claude_estimate) - 0.1,
                            price_history=[],
                        )
                    else:
                        vote = strategy.evaluate(
                            claude_estimate=thesis.claude_estimate,
                            market_price=float(thesis.claude_estimate) - 0.1,
                            related_markets=[],
                        )
                    votes.append(vote)

                from decimal import Decimal
                position = executor.execute(
                    thesis=thesis,
                    votes=votes,
                    market_price=Decimal(str(round(thesis.claude_estimate - 0.1, 4))),
                )
                if position:
                    position_repo.insert(position)
                    market_repo.update_status(thesis.market_id, MarketStatus.TRADED)

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
        """Poll open positions, check exit triggers."""
        while queues.shutdown.empty():
            try:
                open_positions = position_repo.get_open()
                for pos in open_positions:
                    # In paper mode, fetch current price from Polymarket
                    reason = exit_monitor.check_exit(
                        entry_price=pos["entry_price"],
                        target_price=pos["target_price"],
                        current_price=pos["current_price"],
                        volume_10min=0,  # TODO: fetch real volume
                        avg_volume_10min=1,
                        hours_since_entry=0,  # TODO: calculate from opened_at
                    )
                    if reason:
                        pnl = exit_monitor.calculate_pnl(
                            entry_price=pos["entry_price"],
                            exit_price=pos["current_price"],
                            position_size=pos["position_size"],
                            side=pos["side"],
                        )
                        position_repo.close(pos["id"], reason, pnl, pos["current_price"])
                        logger.info("CLOSED %s — %s pnl=$%.2f", pos["id"], reason.value, pnl)

                time.sleep(60)  # Check every minute
            except Exception:
                logger.exception("Exit monitor error")
                time.sleep(60)

    # --- Spawn workers ---
    n_scanner = pool.compute_workers("scanner", 3, settings.scanner_workers)
    n_brain = pool.compute_workers("brain", 6, settings.brain_workers)
    n_executor = pool.compute_workers("executor", 24, settings.executor_workers)
    n_exit = pool.compute_workers("exit_monitor", 6, settings.exit_workers)

    pool.spawn("scanner", scanner_worker, n_scanner)
    pool.spawn("brain", brain_worker, n_brain)
    pool.spawn("executor", executor_worker, n_executor)
    pool.spawn("exit_monitor", exit_monitor_worker, n_exit)

    logger.info(
        "All workers started: %d scanner, %d brain, %d executor, %d exit",
        n_scanner, n_brain, n_executor, n_exit,
    )

    # Graceful shutdown
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


if __name__ == "__main__":
    run()
