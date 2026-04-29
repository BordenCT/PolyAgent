"""Re-export shim. Real implementation lives in polyagent.services.quant.core.estimator."""
from polyagent.services.quant.core.estimator import estimate_up_probability

__all__ = ["estimate_up_probability"]
