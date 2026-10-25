"""Read short SSH commands within a wall-clock deadline."""

import threading
import time
from contextlib import contextmanager


class SSHCommandError(ValueError):
    pass


@contextmanager
def command_deadline(ssh, timeout):
    """Close the SSH transport if any command operation exceeds the total deadline."""
    deadline = time.monotonic() + timeout
    expired = threading.Event()

    def expire():
        expired.set()
        # Closing the transport also interrupts an unanswered exec request.
        ssh.close()

    timer = threading.Timer(timeout, expire)
    timer.daemon = True
    timer.start()
    try:
        yield deadline
    except Exception:
        if expired.is_set() or time.monotonic() >= deadline:
            if not expired.is_set():
                expire()
            raise TimeoutError("SSH command timed out") from None
        raise
    finally:
        if time.monotonic() >= deadline and not expired.is_set():
            expire()
        timer.cancel()
        timer.join()
        if expired.is_set():
            raise TimeoutError("SSH command timed out") from None


def read_command(ssh, command, *, timeout, limit=256):
    """Return bounded stdout after EOF and exit status arrive before the deadline."""
    with command_deadline(ssh, timeout) as deadline:
        channel = None
        try:
            transport = ssh.get_transport()
            channel = transport.open_session(timeout=max(0.001, deadline - time.monotonic()))
            channel.settimeout(max(0.001, deadline - time.monotonic()))
            channel.exec_command(command)
            output = bytearray()
            received = 0
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("SSH command timed out")
                progress = False
                for ready, receive, keep in (
                    (channel.recv_ready, channel.recv, True),
                    (channel.recv_stderr_ready, channel.recv_stderr, False),
                ):
                    if ready():
                        chunk = receive(min(4096, limit + 1 - received))
                        received += len(chunk)
                        if received > limit:
                            raise SSHCommandError("SSH command output exceeds limit")
                        if keep:
                            output.extend(chunk)
                        progress = True
                if channel.eof_received and not channel.recv_ready() and not channel.recv_stderr_ready():
                    if channel.exit_status_ready():
                        if channel.recv_exit_status() != 0:
                            raise SSHCommandError("SSH command failed")
                        return bytes(output)
                if channel.closed:
                    raise SSHCommandError("SSH command closed without a complete result")
                if not progress:
                    time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        finally:
            if channel is not None:
                channel.close()
