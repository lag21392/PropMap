from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, TypeVar, Any

T = TypeVar("T")

# Global ThreadPoolExecutor for CPU-bound work
# Workers = CPU count for CPU-bound tasks
_cpu_executor: ThreadPoolExecutor | None = None

# Global ThreadPoolExecutor for I/O-bound work
# Workers = 20-30 for I/O-bound tasks (network, disk)
_io_executor: ThreadPoolExecutor | None = None


def get_cpu_executor() -> ThreadPoolExecutor:
    """Get or create the global CPU-bound ThreadPoolExecutor."""
    global _cpu_executor
    if _cpu_executor is None:
        cpu_count = os.cpu_count() or 4
        _cpu_executor = ThreadPoolExecutor(max_workers=cpu_count, thread_name_prefix="cpu-pool")
    return _cpu_executor


def get_io_executor() -> ThreadPoolExecutor:
    """Get or create the global I/O-bound ThreadPoolExecutor."""
    global _io_executor
    if _io_executor is None:
        _io_executor = ThreadPoolExecutor(max_workers=30, thread_name_prefix="io-pool")
    return _io_executor


def shutdown_executors() -> None:
    """Shutdown both global executors."""
    global _cpu_executor, _io_executor
    if _cpu_executor is not None:
        _cpu_executor.shutdown(wait=True)
        _cpu_executor = None
    if _io_executor is not None:
        _io_executor.shutdown(wait=True)
        _io_executor = None


def run_in_cpu_pool(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a CPU-bound function in the global CPU pool."""
    executor = get_cpu_executor()
    future = executor.submit(func, *args, **kwargs)
    return future.result()


def run_in_io_pool(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run an I/O-bound function in the global I/O pool."""
    executor = get_io_executor()
    future = executor.submit(func, *args, **kwargs)
    return future.result()


def submit_to_cpu_pool(func: Callable[..., T], *args: Any, **kwargs: Any):
    """Submit a CPU-bound function to the global CPU pool (returns Future)."""
    executor = get_cpu_executor()
    return executor.submit(func, *args, **kwargs)


def submit_to_io_pool(func: Callable[..., T], *args: Any, **kwargs: Any):
    """Submit an I/O-bound function to the global I/O pool (returns Future)."""
    executor = get_io_executor()
    return executor.submit(func, *args, **kwargs)