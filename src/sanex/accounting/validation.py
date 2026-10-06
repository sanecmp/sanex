"""Shared validation for accounting operations."""

from ..exceptions import AccountingError


def require_nonnegative(value: int, name: str) -> None:
    """Require a nonnegative integer value, excluding booleans."""

    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AccountingError(f"{name} is invalid")
