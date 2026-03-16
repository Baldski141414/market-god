"""
Adaptive Learning Engine.
Tracks signal component accuracy daily and adjusts weights.
Runs once every 24 hours.
"""
import time
import threading
import math
from core.data_store import store
from core.config import DEFAULT_WEIGHTS
from signals.weights import set_weights

_24H = 86400


def _evaluate_accuracy():
    """
    Look at recent closed trades and infer which components contributed
    to correct vs incorrect predictions.
    Returns dict of component -> accuracy_score (0.0-1.0).
    """
    trades = store.portfolio.get('trades', [])
    closed = [t for t in trades if t.get('pnl') is not None]

    if len(closed) < 10:
        # Not enough data — return equal weight signal
        return {k: 1.0 for k in DEFAULT_WEIGHTS}

    component_scores: dict[str, list[float]] = {k: [] for k in DEFAULT_WEIGHTS}

    for trade in closed[-100:]:  # last 100 trades
        was_win = trade.get('pnl', 0) > 0
        signal_score = trade.get('score', 50) or 50

        for comp in DEFAULT_WEIGHTS:
            # Proxy: if signal was confident AND trade won, reward all components
            # If signal was confident AND trade lost, penalize
            contributed = signal_score >= 70  # was this a high-confidence call?
            if contributed:
                component_scores[comp].append(1.0 if was_win else 0.0)

    accuracy = {}
    for comp, scores in component_scores.items():
        if scores:
            accuracy[comp] = sum(scores) / len(scores)
        else:
            accuracy[comp] = 0.5  # neutral

    return accuracy


def _rebalance_weights(accuracy: dict[str, float]) -> dict[str, float]:
    """
    Adjust weights proportional to accuracy.
    Better-performing signals get more weight.
    Worst performers get minimum 20% of base weight.
    """
    base = dict(DEFAULT_WEIGHTS)
    new_weights = {}

    for comp, base_w in base.items():
        acc = accuracy.get(comp, 0.5)
        # Accuracy 0.5 = neutral (no change), 1.0 = double weight, 0.0 = halve
        multiplier = 0.5 + acc  # range: 0.5 - 1.5
        new_weights[comp] = base_w * multiplier

    # Normalize to sum to 1.0
    total = sum(new_weights.values())
    normalized = {k: v / total for k, v in new_weights.items()}

    # Clamp: no weight below 50% or above 200% of default
    for comp in normalized:
        default = base[comp]
        normalized[comp] = max(default * 0.5, min(default * 2.0, normalized[comp]))

    # Re-normalize after clamping
    total2 = sum(normalized.values())
    return {k: round(v / total2, 4) for k, v in normalized.items()}


def _learning_loop():
    # Wait until some trades have happened
    time.sleep(3600)  # wait 1 hour before first evaluation

    while True:
        try:
            accuracy = _evaluate_accuracy()
            new_weights = _rebalance_weights(accuracy)
            set_weights(new_weights)
            print(f'[Adaptive] weights updated: {new_weights}')
            store.signal_accuracy = accuracy
        except Exception as e:
            print(f'[Adaptive] error: {e}')
        time.sleep(_24H)


def start_adaptive_learning():
    t = threading.Thread(target=_learning_loop, daemon=True, name='adaptive')
    t.start()
    print('[Adaptive] learning engine started (first update in 1h)')
