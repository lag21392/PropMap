"""Rate limiting and circuit breaker for HTTP requests.

Provides thread-safe rate limiting per host and circuit breaker pattern
to prevent cascading failures when hosts are down or rate limiting us.
"""

from __future__ import annotations

import time
import threading
from dataclasses import dataclass, field
from typing import Dict


@dataclass
class RateLimitConfig:
    """Configuration for rate limiting per host."""
    requests_per_minute: int = 30
    burst_size: int = 5
    window_seconds: float = 60.0


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker per host."""
    failure_threshold: int = 5
    recovery_timeout: float = 60.0  # seconds
    half_open_requests: int = 3


class SlidingWindowRateLimiter:
    """Sliding window rate limiter with burst support."""
    
    def __init__(self, config: RateLimitConfig | None = None):
        self.config = config or RateLimitConfig()
        self._lock = threading.Lock()
        self._windows: Dict[str, list[float]] = {}
    
    def allow(self, key: str) -> bool:
        """Check if request is allowed for key. Returns True if allowed."""
        now = time.monotonic()
        window_start = now - self.config.window_seconds
        
        with self._lock:
            # Clean old entries
            timestamps = self._windows.get(key, [])
            timestamps = [t for t in timestamps if t > window_start]
            
            # Check rate limit
            if len(timestamps) >= self.config.requests_per_minute:
                self._windows[key] = timestamps
                return False
            
            # Allow request
            timestamps.append(now)
            self._windows[key] = timestamps
            return True
    
    def wait_time(self, key: str) -> float:
        """Return seconds to wait before next request is allowed."""
        now = time.monotonic()
        window_start = now - self.config.window_seconds
        
        with self._lock:
            timestamps = self._windows.get(key, [])
            timestamps = [t for t in timestamps if t > window_start]
            
            if len(timestamps) < self.config.requests_per_minute:
                return 0.0
            
            # Time until oldest request expires
            oldest = min(timestamps)
            wait = oldest + self.config.window_seconds - now
            return max(0.0, wait)
    
    def reset(self, key: str | None = None) -> None:
        """Reset rate limit for key or all keys."""
        with self._lock:
            if key:
                self._windows.pop(key, None)
            else:
                self._windows.clear()


@dataclass
class CircuitState:
    """Circuit breaker state for a host."""
    failures: int = 0
    last_failure: float = 0.0
    state: str = "closed"  # closed, open, half_open
    success_count: int = 0
    opened_at: float = 0.0


class CircuitBreaker:
    """Circuit breaker pattern for HTTP requests."""
    
    def __init__(self, config: CircuitBreakerConfig | None = None):
        self.config = config or CircuitBreakerConfig()
        self._lock = threading.Lock()
        self._states: Dict[str, CircuitState] = {}
    
    def can_execute(self, key: str) -> bool:
        """Check if request can be executed for key."""
        now = time.monotonic()
        
        with self._lock:
            state = self._states.get(key, CircuitState())
            
            if state.state == "closed":
                return True
            
            if state.state == "open":
                # Check if recovery timeout passed
                if now - state.opened_at >= self.config.recovery_timeout:
                    state.state = "half_open"
                    state.success_count = 0
                    self._states[key] = state
                    return True
                return False
            
            if state.state == "half_open":
                # Allow limited requests in half-open state
                return state.success_count < self.config.half_open_requests
            
            return True
    
    def record_success(self, key: str) -> None:
        """Record successful request."""
        with self._lock:
            state = self._states.get(key, CircuitState())
            
            if state.state == "half_open":
                state.success_count += 1
                if state.success_count >= self.config.half_open_requests:
                    state.state = "closed"
                    state.failures = 0
                    state.success_count = 0
            
            elif state.state == "open":
                # Shouldn't happen, but handle gracefully
                pass
            
            else:  # closed
                state.failures = max(0, state.failures - 1)
            
            self._states[key] = state
    
    def record_failure(self, key: str) -> None:
        """Record failed request."""
        now = time.monotonic()
        
        with self._lock:
            state = self._states.get(key, CircuitState())
            state.failures += 1
            state.last_failure = now
            
            if state.state == "half_open":
                # Failure in half-open -> back to open
                state.state = "open"
                state.opened_at = now
                state.success_count = 0
            
            elif state.state == "closed":
                if state.failures >= self.config.failure_threshold:
                    state.state = "open"
                    state.opened_at = now
            
            self._states[key] = state
    
    def get_state(self, key: str) -> str:
        """Get current circuit breaker state for key."""
        with self._lock:
            state = self._states.get(key, CircuitState())
            return state.state
    
    def get_failures(self, key: str) -> int:
        """Get failure count for key."""
        with self._lock:
            state = self._states.get(key, CircuitState())
            return state.failures
    
    def reset(self, key: str | None = None) -> None:
        """Reset circuit breaker for key or all keys."""
        with self._lock:
            if key:
                self._states.pop(key, None)
            else:
                self._states.clear()


# Global instances
_rate_limiter = SlidingWindowRateLimiter(RateLimitConfig(
    requests_per_minute=30,
    burst_size=5,
    window_seconds=60.0
))

_circuit_breaker = CircuitBreaker(CircuitBreakerConfig(
    failure_threshold=5,
    recovery_timeout=120.0,
    half_open_requests=3
))


def rate_limiter() -> SlidingWindowRateLimiter:
    """Get global rate limiter instance."""
    return _rate_limiter


def circuit_breaker() -> CircuitBreaker:
    """Get global circuit breaker instance."""
    return _circuit_breaker


def reset_rate_limit() -> None:
    """Reset all rate limiting state."""
    _rate_limiter.reset()


def reset_circuit_breaker() -> None:
    """Reset all circuit breaker state."""
    _circuit_breaker.reset()


def reset_all() -> None:
    """Reset both rate limiter and circuit breaker."""
    reset_rate_limit()
    reset_circuit_breaker()
