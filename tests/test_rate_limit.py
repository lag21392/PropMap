"""Tests for rate limiting and circuit breaker."""

import time
import pytest
from app.rate_limit import (
    SlidingWindowRateLimiter,
    CircuitBreaker,
    RateLimitConfig,
    CircuitBreakerConfig,
    rate_limiter,
    circuit_breaker
)


class TestSlidingWindowRateLimiter:
    """Test sliding window rate limiter."""

    def test_allow_within_limit(self):
        """Should allow requests within limit."""
        config = RateLimitConfig(requests_per_minute=5, window_seconds=60)
        limiter = SlidingWindowRateLimiter(config)
        
        for _ in range(5):
            assert limiter.allow("test_host")
        
        # 6th request should be denied
        assert not limiter.allow("test_host")

    def test_different_hosts_independent(self):
        """Different hosts should have independent limits."""
        config = RateLimitConfig(requests_per_minute=2, window_seconds=60)
        limiter = SlidingWindowRateLimiter(config)
        
        assert limiter.allow("host1")
        assert limiter.allow("host1")
        assert not limiter.allow("host1")
        
        # host2 should still be allowed
        assert limiter.allow("host2")
        assert limiter.allow("host2")

    def test_window_resets(self):
        """Window should reset after time passes."""
        config = RateLimitConfig(requests_per_minute=1, window_seconds=1)
        limiter = SlidingWindowRateLimiter(config)
        
        assert limiter.allow("test_host")
        assert not limiter.allow("test_host")
        
        time.sleep(1.1)
        assert limiter.allow("test_host")

    def test_wait_time_calculation(self):
        """Wait time should be calculated correctly."""
        config = RateLimitConfig(requests_per_minute=1, window_seconds=10)
        limiter = SlidingWindowRateLimiter(config)
        
        limiter.allow("test_host")
        wait = limiter.wait_time("test_host")
        assert wait > 0
        assert wait <= 10

    def test_reset_clears_history(self):
        """Reset should clear request history."""
        config = RateLimitConfig(requests_per_minute=1, window_seconds=60)
        limiter = SlidingWindowRateLimiter(config)
        
        limiter.allow("test_host")
        assert not limiter.allow("test_host")
        
        limiter.reset("test_host")
        assert limiter.allow("test_host")

    def test_custom_config(self):
        """Should respect custom configuration."""
        config = RateLimitConfig(requests_per_minute=10, window_seconds=120)
        limiter = SlidingWindowRateLimiter(config)
        
        for _ in range(10):
            assert limiter.allow("host")
        
        assert not limiter.allow("host")


class TestCircuitBreaker:
    """Test circuit breaker pattern."""

    def test_initial_state_closed(self):
        """Circuit breaker should start in CLOSED state."""
        config = CircuitBreakerConfig(failure_threshold=5, recovery_timeout=60)
        cb = CircuitBreaker(config)
        
        assert cb.get_state("test_host") == "closed"
        assert cb.can_execute("test_host")

    def test_record_success_keeps_closed(self):
        """Success should keep circuit closed."""
        config = CircuitBreakerConfig(failure_threshold=3, recovery_timeout=60)
        cb = CircuitBreaker(config)
        
        cb.record_success("test_host")
        assert cb.get_state("test_host") == "closed"

    def test_failures_open_circuit(self):
        """Failures should open circuit after threshold."""
        config = CircuitBreakerConfig(failure_threshold=3, recovery_timeout=60)
        cb = CircuitBreaker(config)
        
        for _ in range(3):
            cb.record_failure("test_host")
        
        assert cb.get_state("test_host") == "open"
        assert not cb.can_execute("test_host")

    def test_half_open_after_timeout(self):
        """Circuit should go to HALF_OPEN after timeout."""
        config = CircuitBreakerConfig(failure_threshold=2, recovery_timeout=1)
        cb = CircuitBreaker(config)
        
        cb.record_failure("test_host")
        cb.record_failure("test_host")
        assert cb.get_state("test_host") == "open"
        
        time.sleep(1.1)
        # State transitions on can_execute call
        assert cb.can_execute("test_host")
        assert cb.get_state("test_host") == "half_open"

    def test_half_open_success_closes_circuit(self):
        """Success in HALF_OPEN should close circuit."""
        config = CircuitBreakerConfig(failure_threshold=2, recovery_timeout=1, half_open_requests=1)
        cb = CircuitBreaker(config)
        
        cb.record_failure("test_host")
        cb.record_failure("test_host")
        time.sleep(1.1)
        
        # Trigger transition to half_open
        assert cb.can_execute("test_host")
        assert cb.get_state("test_host") == "half_open"
        
        cb.record_success("test_host")
        assert cb.get_state("test_host") == "closed"

    def test_half_open_failure_reopens(self):
        """Failure in HALF_OPEN should reopen circuit."""
        config = CircuitBreakerConfig(failure_threshold=2, recovery_timeout=1)
        cb = CircuitBreaker(config)
        
        cb.record_failure("test_host")
        cb.record_failure("test_host")
        time.sleep(1.1)
        
        cb.record_failure("test_host")
        assert cb.get_state("test_host") == "open"

    def test_different_hosts_independent(self):
        """Different hosts should have independent circuits."""
        config = CircuitBreakerConfig(failure_threshold=2, recovery_timeout=60)
        cb = CircuitBreaker(config)
        
        cb.record_failure("host1")
        cb.record_failure("host1")
        assert cb.get_state("host1") == "open"
        assert cb.get_state("host2") == "closed"

    def test_success_resets_failure_count(self):
        """Success should reset failure count."""
        config = CircuitBreakerConfig(failure_threshold=3, recovery_timeout=60)
        cb = CircuitBreaker(config)
        
        cb.record_failure("test_host")
        cb.record_failure("test_host")
        cb.record_success("test_host")
        # Success in closed state decrements failures from 2 to 1
        assert cb.get_failures("test_host") == 1
        
        cb.record_failure("test_host")
        # Should still be closed (only 2 failures total)
        assert cb.get_state("test_host") == "closed"
        assert cb.get_failures("test_host") == 2
        
        # One more failure should open circuit
        cb.record_failure("test_host")
        assert cb.get_state("test_host") == "open"


class TestGlobalInstances:
    """Test global rate limiter and circuit breaker instances."""

    def test_rate_limiter_global(self):
        """Global rate limiter should be accessible."""
        rl = rate_limiter()
        assert isinstance(rl, SlidingWindowRateLimiter)

    def test_circuit_breaker_global(self):
        """Global circuit breaker should be accessible."""
        cb = circuit_breaker()
        assert isinstance(cb, CircuitBreaker)

    def test_global_instances_work(self):
        """Global instances should work correctly."""
        rl = rate_limiter()
        cb = circuit_breaker()
        
        # Test rate limiter
        assert rl.allow("test_global")
        
        # Test circuit breaker
        assert cb.can_execute("test_global")
        cb.record_success("test_global")
        assert cb.get_state("test_global") == "closed"


class TestIntegration:
    """Integration tests with http_client."""

    def test_rate_limit_integration(self):
        """Rate limiting should integrate with http_client."""
        from app import rate_limit
        
        rl = rate_limit.rate_limiter()
        # Use very low limit for testing
        config = RateLimitConfig(requests_per_minute=1, window_seconds=60)
        test_rl = SlidingWindowRateLimiter(config)
        
        assert test_rl.allow("test.com")
        assert not test_rl.allow("test.com")
        test_rl.reset("test.com")
        assert test_rl.allow("test.com")

    def test_circuit_breaker_integration(self):
        """Circuit breaker should integrate with http_client."""
        from app import rate_limit
        
        cb = rate_limit.circuit_breaker()
        config = CircuitBreakerConfig(failure_threshold=2, recovery_timeout=60)
        test_cb = CircuitBreaker(config)
        
        assert test_cb.can_execute("test.com")
        test_cb.record_failure("test.com")
        test_cb.record_failure("test.com")
        assert not test_cb.can_execute("test.com")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
