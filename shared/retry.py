"""
shared/retry.py

Reusable retry decorator with exponential backoff.
Modules that perform network calls or otherwise flaky operations
should wrap the relevant function with `@retry(...)` instead of
implementing their own retry loops.
"""

from __future__ import annotations

import functools
import time
from typing import Any, Callable, Tuple, Type, TypeVar

from shared.logger import get_logger

logger = get_logger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


def retry(
    max_attempts: int = 3,
    base_delay_seconds: float = 1.0,
    backoff_multiplier: float = 2.0,
    exceptions: Tuple[Type[BaseException], ...] = (Exception,),
) -> Callable[[F], F]:
    """
    Decorator factory that retries a function call on failure.

    Args:
        max_attempts: Maximum number of attempts before giving up.
        base_delay_seconds: Initial delay between retries, in seconds.
        backoff_multiplier: Multiplier applied to the delay after each
            failed attempt (exponential backoff).
        exceptions: Tuple of exception types that should trigger a retry.

    Returns:
        A decorator that wraps the target function with retry logic.
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            attempt = 1
            delay = base_delay_seconds

            while attempt <= max_attempts:
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:  # noqa: BLE001 - intentional broad catch
                    if attempt == max_attempts:
                        logger.error(
                            "Function '%s' failed after %d attempts: %s",
                            func.__name__,
                            max_attempts,
                            exc,
                        )
                        raise

                    logger.warning(
                        "Function '%s' failed on attempt %d/%d: %s. "
                        "Retrying in %.1fs...",
                        func.__name__,
                        attempt,
                        max_attempts,
                        exc,
                        delay,
                    )
                    time.sleep(delay)
                    delay *= backoff_multiplier
                    attempt += 1

            # Unreachable, but keeps type checkers happy.
            raise RuntimeError(f"Retry loop exited unexpectedly for '{func.__name__}'")

        return wrapper  # type: ignore[return-value]

    return decorator
