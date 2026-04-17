"""Tests for the OllamaEstimator in-memory + on-disk cache."""
import json
from unittest.mock import MagicMock, patch


@patch("polyagent.data.clients.ollama.OllamaClient")
def test_cache_hit_skips_llm_call(mock_client_cls, tmp_path):
    from polyagent.backtest.estimator import OllamaEstimator

    mock_client = MagicMock()
    mock_client.estimate_probability.return_value = 0.72
    mock_client_cls.return_value = mock_client

    cache_path = tmp_path / "cache.json"
    est = OllamaEstimator(cache_path=cache_path)

    first = est.estimate("0x1", question="Will X?", market_price=0.5)
    second = est.estimate("0x1", question="Will X?", market_price=0.9)  # different price

    assert first == 0.72
    assert second == 0.72  # cached — LLM not re-called
    assert mock_client.estimate_probability.call_count == 1


@patch("polyagent.data.clients.ollama.OllamaClient")
def test_cache_persists_to_disk_on_flush(mock_client_cls, tmp_path):
    from polyagent.backtest.estimator import OllamaEstimator

    mock_client = MagicMock()
    mock_client.estimate_probability.return_value = 0.55
    mock_client_cls.return_value = mock_client

    cache_path = tmp_path / "cache.json"
    est = OllamaEstimator(cache_path=cache_path)
    est.estimate("0x1", question="Will X?", market_price=0.5)
    est.flush()

    assert cache_path.exists()
    saved = json.loads(cache_path.read_text())
    assert saved == {"0x1": 0.55}


@patch("polyagent.data.clients.ollama.OllamaClient")
def test_cache_loaded_from_disk_on_init(mock_client_cls, tmp_path):
    from polyagent.backtest.estimator import OllamaEstimator

    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client

    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps({"0xabc": 0.91}))

    est = OllamaEstimator(cache_path=cache_path)
    prob = est.estimate("0xabc", question="Will Y?", market_price=0.5)

    assert prob == 0.91
    mock_client.estimate_probability.assert_not_called()


@patch("polyagent.data.clients.ollama.OllamaClient")
def test_auto_flush_after_threshold(mock_client_cls, tmp_path):
    from polyagent.backtest.estimator import OllamaEstimator

    mock_client = MagicMock()
    mock_client.estimate_probability.return_value = 0.5
    mock_client_cls.return_value = mock_client

    cache_path = tmp_path / "cache.json"
    est = OllamaEstimator(cache_path=cache_path)
    est.FLUSH_EVERY = 3

    for i in range(3):
        est.estimate(f"0x{i}", question=f"Q{i}?", market_price=0.5)

    assert cache_path.exists()
    saved = json.loads(cache_path.read_text())
    assert len(saved) == 3


@patch("polyagent.data.clients.ollama.OllamaClient")
def test_missing_question_falls_back_without_caching(mock_client_cls, tmp_path):
    from polyagent.backtest.estimator import OllamaEstimator

    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client

    cache_path = tmp_path / "cache.json"
    est = OllamaEstimator(cache_path=cache_path)
    prob = est.estimate("0x1", question="", market_price=0.42)

    assert prob == 0.42
    mock_client.estimate_probability.assert_not_called()
    # No cache entry written for unanswerable questions
    est.flush()
    assert not cache_path.exists() or json.loads(cache_path.read_text()) == {}


@patch("polyagent.data.clients.ollama.OllamaClient")
def test_corrupt_cache_file_is_tolerated(mock_client_cls, tmp_path):
    from polyagent.backtest.estimator import OllamaEstimator

    mock_client = MagicMock()
    mock_client.estimate_probability.return_value = 0.6
    mock_client_cls.return_value = mock_client

    cache_path = tmp_path / "cache.json"
    cache_path.write_text("{{{not json")

    est = OllamaEstimator(cache_path=cache_path)
    prob = est.estimate("0x1", question="Will?", market_price=0.5)
    assert prob == 0.6
