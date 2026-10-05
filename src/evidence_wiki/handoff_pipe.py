"""Synchronous finite pipe delivery under an already owned native lease."""

from __future__ import annotations

import math
import os
import select
import stat
import time

MAX_BYTES = 1_048_576
MAX_SECONDS = 30
CHUNK_BYTES = 4096
WAIT_SECONDS = 0.01


class HandoffPipeError(ValueError):
    """Pipe mechanics failed; this says nothing about usage or no effects."""


def _fail(reason):
    raise HandoffPipeError("handoff_pipe_" + reason)


def write_handoff(pipe, payload, lease, *, deadline, max_bytes=MAX_BYTES, is_alive=None):
    """Write exact immutable bytes in this thread, then complete the native lease.

    The caller owns process, accounting and native source custody. No failure
    implies zero usage or absence of earlier disclosure. This helper never starts
    a thread, invokes a provider, acknowledges model comprehension or retries an
    attempt. It closes the selected stream on every exit, including partial IO.
    """
    failure = None
    try:
        started = time.monotonic()
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BYTES:
            _fail("byte_limit_invalid")
        if type(payload) is not bytes or not 1 <= len(payload) <= max_bytes:
            _fail("payload_invalid")
        if (type(deadline) not in {int, float} or not math.isfinite(deadline)
                or deadline > started + MAX_SECONDS):
            _fail("deadline_invalid")
        if is_alive is not None and not callable(is_alive):
            _fail("process_check_invalid")
        try:
            descriptor = pipe.fileno()
            info = os.fstat(descriptor)
            if type(descriptor) is not int or not stat.S_ISFIFO(info.st_mode):
                _fail("pipe_invalid")
            identity = info.st_dev, info.st_ino
            os.set_blocking(descriptor, False)
        except (OSError, ValueError) as error:
            if isinstance(error, HandoffPipeError):
                raise
            _fail("pipe_unavailable")

        def checkpoint():
            if time.monotonic() >= deadline:
                _fail("deadline_expired")
            lease.checkpoint()
            if time.monotonic() >= deadline:
                _fail("deadline_expired")
            if is_alive is not None and not is_alive():
                _fail("child_exit")
            try:
                current = os.fstat(descriptor)
            except OSError:
                _fail("pipe_unavailable")
            if (current.st_dev, current.st_ino) != identity:
                _fail("pipe_changed")

        offset = 0
        while offset < len(payload):
            checkpoint()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _fail("deadline_expired")
            try:
                _readable, writable, _exceptional = select.select([], [descriptor], [], min(WAIT_SECONDS, remaining))
            except InterruptedError:
                continue
            except (OSError, ValueError):
                _fail("pipe_wait_failed")
            if not writable:
                continue
            # Permission/expiry and monotonic time are checked after waiting,
            # immediately before each actual write, including short retries.
            checkpoint()
            try:
                count = os.write(descriptor, payload[offset:offset + CHUNK_BYTES])
            except (BlockingIOError, InterruptedError):
                continue
            except BrokenPipeError:
                _fail("pipe_broken")
            except OSError:
                _fail("pipe_write_failed")
            if type(count) is not int or not 1 <= count <= min(CHUNK_BYTES, len(payload) - offset):
                _fail("pipe_write_invalid")
            offset += count
        checkpoint()
        return lease.complete(payload)
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            pipe.close()
        except (OSError, ValueError):
            if failure is None:
                _fail("pipe_close_failed")
