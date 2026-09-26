# Rate Limiting and Circuit Breaker Implementation

## Overview
Implemented rate limiting and circuit breaker pattern for HTTP requests in PropMap to prevent cascading failures and respect host rate limits.

## Changes Made

### 1. New Module: `app/rate_limit.py`
Created comprehensive rate limiting and circuit breaker module with:
- **SlidingWindowRateLimiter**: Thread-safe rate limiting per host with sliding window algorithm
  - Configurable requests per minute (default: 30)
  - Window-based tracking with automatic cleanup
  - Burst support via burst_size parameter
  - Wait time calculation for rate limited hosts

- **CircuitBreaker**: Circuit breaker pattern for HTTP requests
  - States: closed, open, half_open
  - Configurable failure threshold (default: 5)
  - Recovery timeout (default: 60s)
  - Half-open state for testing recovery
  - Per-host tracking

- **Global instances**: `rate_limiter()` and `circuit_breaker()` for easy access

### 2. Integration with `app/http_client.py`
Modified `fetch_text()` function to:
- Check circuit breaker before making requests
- Check rate limiter before making requests
- Raise RuntimeError with descriptive messages when limits exceeded

Modified `_remember_ok()` to record successful requests in circuit breaker

Modified `_mark_blocked()` to record failures in circuit breaker

### 3. Tests: `tests/test_rate_limit.py`
Created comprehensive test suite with 19 tests:
- Sliding window rate limiter tests (6 tests)
- Circuit breaker tests (8 tests)
- Global instances tests (3 tests)
- Integration tests (2 tests)

All tests passing ✅

## Configuration

### Rate Limiter Defaults
- requests_per_minute: 30
- burst_size: 5
- window_seconds: 60

### Circuit Breaker Defaults
- failure_threshold: 5
- recovery_timeout: 60 seconds
- half_open_requests: 3

## Benefits

1. **Prevents cascading failures**: Circuit breaker opens after repeated failures, preventing overload
2. **Respects rate limits**: Sliding window prevents hitting host rate limits
3. **Automatic recovery**: Circuit breaker transitions to half-open after timeout
4. **Per-host isolation**: Different hosts have independent limits and circuit states
5. **Thread-safe**: All operations protected by threading.Lock
6. **Non-blocking**: Rate limiting raises exceptions instead of blocking threads

## Usage Example

```python
from app import rate_limit

# Get instances
rl = rate_limit.rate_limiter()
cb = rate_limit.circuit_breaker()

# Check before request
if not cb.can_execute(host):
    raise RuntimeError("Circuit breaker open")

if not rl.allow(host):
    wait = rl.wait_time(host)
    raise RuntimeError(f"Rate limited, wait {wait}s")

# Make request
try:
    response = fetch(url)
    cb.record_success(host)
except Exception:
    cb.record_failure(host)
    raise
```

## Testing
Run tests:
```bash
python3 -m pytest tests/test_rate_limit.py -v
```

All 19 tests pass successfully.

## Integration Points
- `app/http_client.py`: Main integration point for HTTP requests
- `app/rate_limit.py`: Core implementation
- Existing host blocking (`_blocked_until`) works alongside circuit breaker
- Existing backoff mechanisms complement rate limiting

## Future Enhancements
1. Metrics/logging for rate limit hits and circuit breaker state changes
2. Configurable per-host limits via config file
3. Dashboard for monitoring circuit breaker states
4. Adaptive rate limiting based on response times
5. Integration with existing `_allow` bucket rate limiting in accounts.py
