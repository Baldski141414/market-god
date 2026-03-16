"""Kelly Criterion position sizing."""


def kelly_fraction(win_rate: float, avg_win: float, avg_loss: float) -> float:
    """
    Full Kelly fraction.
    win_rate: 0.0 - 1.0
    avg_win / avg_loss: positive floats (e.g. 0.08 and 0.03)
    Returns fraction of capital to risk (0.0 - 1.0), capped at 0.25.
    """
    if avg_loss <= 0 or win_rate <= 0 or win_rate >= 1:
        return 0.05  # default 5%
    b = avg_win / avg_loss
    q = 1 - win_rate
    k = (b * win_rate - q) / b
    # Half-Kelly for safety, capped at 25%
    return max(0.01, min(0.25, k * 0.5))


def position_size(portfolio_value: float, win_rate: float,
                  avg_win: float = 0.08, avg_loss: float = 0.03) -> float:
    """Returns dollar amount to invest in a single position."""
    fraction = kelly_fraction(win_rate, avg_win, avg_loss)
    return portfolio_value * fraction
