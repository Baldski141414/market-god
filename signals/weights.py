"""
Adaptive signal weights.
Loaded from store at runtime; updated every 24 hours by the learning module.
"""
from core.config import DEFAULT_WEIGHTS
from core.data_store import store


def get_weights() -> dict[str, float]:
    """Return current weights from store (or defaults if not yet set)."""
    w = store.weights
    if not w:
        return dict(DEFAULT_WEIGHTS)
    # Normalize to ensure they sum to 1.0
    total = sum(w.values())
    if total == 0:
        return dict(DEFAULT_WEIGHTS)
    return {k: v / total for k, v in w.items()}


def set_weights(new_weights: dict[str, float]):
    """Update weights in store (learning module calls this)."""
    import time
    store.weights = dict(new_weights)
    store.weights_updated_at = time.time()
