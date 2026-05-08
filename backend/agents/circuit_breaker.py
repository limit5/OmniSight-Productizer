"""Circuit breakers for runner external services.

The JIRA runner polls every tick.  When JIRA, Gerrit, or backend REST is
down, repeated retries add noise without making progress.  These
process-local breakers pause the runner after five consecutive transport
failures, allow a half-open probe after 60s, and notify the operator on
open transitions.
"""

from __future__ import annotations

import logging
import time
import urllib.error
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)


class CircuitBreakerOpen(RuntimeError):
    """Raised when a service circuit is open inside its recovery window."""


def _notify_operator(service: str) -> None:
    try:
        from backend.agents.jira_dispatch import notify_operator

        notify_operator(
            channel="runner-alerts",
            severity="high",
            detail=f"{service} unreachable; runner paused",
        )
    except Exception as exc:  # pragma: no cover - stdout fallback only
        log.warning("circuit_breaker_notify_failed service=%s error=%s", service, exc)


@dataclass
class CircuitBreaker:
    name: str
    failure_threshold: int = 5
    recovery_timeout: int = 60
    state: str = "closed"
    consecutive_failures: int = 0
    opened_at: float = 0

    def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        now = time.time()
        if self.state == "open":
            if now - self.opened_at > self.recovery_timeout:
                self.state = "half-open"
            else:
                raise CircuitBreakerOpen(self.name)

        try:
            result = fn(*args, **kwargs)
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                raise
            self._record_failure()
            raise
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            self._record_failure()
            raise exc

        self.consecutive_failures = 0
        if self.state == "half-open":
            self.state = "closed"
            log.info("circuit_breaker_recovered service=%s", self.name)
        return result

    def _record_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.failure_threshold:
            self.state = "open"
            self.opened_at = time.time()
            log.error("circuit_breaker_opened service=%s", self.name)
            _notify_operator(self.name)

    def is_open(self) -> bool:
        return self.state == "open" and time.time() - self.opened_at <= self.recovery_timeout

    def reset(self) -> None:
        self.state = "closed"
        self.consecutive_failures = 0
        self.opened_at = 0


BREAKERS = {
    "gerrit_ssh": CircuitBreaker("gerrit_ssh"),
    "gerrit_rest": CircuitBreaker("gerrit_rest"),
    "jira_rest": CircuitBreaker("jira_rest"),
    "backend_rest": CircuitBreaker("backend_rest"),
}


def open_services() -> list[str]:
    return [breaker.name for breaker in BREAKERS.values() if breaker.is_open()]


def reset_for_tests() -> None:
    for breaker in BREAKERS.values():
        breaker.reset()
