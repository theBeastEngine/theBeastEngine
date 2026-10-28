# services/validator.py
from config.settings import SETTINGS
from core.filter import Ctx, reject_reason


def validate_signal(s) -> bool:
    """One rule set for the whole engine: MIN_EV floor plus the shared filters (fakes, liabilities, traps)."""
    ev = s.get("ev", 0) if isinstance(s, dict) else getattr(s, "ev", 0)
    return (ev or 0) >= SETTINGS.MIN_EV and reject_reason(s, Ctx()) is None
