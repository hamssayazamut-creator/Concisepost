"""
concisepost — local-first inter-agent message optimization.

Re-exports the public SDK surface so callers can simply::

    from concisepost import RefinementEngine, InfiniteLoopException
"""

from concisepost.core import (
    ConcisePostError,
    InfiniteLoopException,
    RefinementError,
    LocalCacheManager,
    CacheRecord,
    LoopDetector,
    PromptCacheOptimizer,
    OptimizedPrompt,
    QualityScorer,
    QualityReport,
    TelemetryReporter,
    TelemetryEvent,
    HttpTelemetrySink,
    RefinementEngine,
    RefinementResult,
    Provider,
    PRICE_TABLE_USD_PER_1K,
    price_for,
    estimate_tokens,
)

__version__ = "1.0.0"

__all__ = [
    "ConcisePostError",
    "InfiniteLoopException",
    "RefinementError",
    "LocalCacheManager",
    "CacheRecord",
    "LoopDetector",
    "PromptCacheOptimizer",
    "OptimizedPrompt",
    "QualityScorer",
    "QualityReport",
    "TelemetryReporter",
    "TelemetryEvent",
    "HttpTelemetrySink",
    "RefinementEngine",
    "RefinementResult",
    "Provider",
    "PRICE_TABLE_USD_PER_1K",
    "price_for",
    "estimate_tokens",
    "__version__",
]
