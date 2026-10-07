"""Reconnecting a dashboard: why the connection went away, how long to keep trying, what to say."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from blamixshell import reconnect as rc  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_error_text_that_means_the_connection_is_gone():
    for gone in ("Socket is closed", "EOF during negotiation", "[Errno 104] Connection reset by peer", "Broken pipe",
                 "SSH session not active", "timed out", "Network is unreachable"):
        assert rc.looks_dropped(gone), gone
    for fine in ("exit code 1", "No such file or directory", "permission denied", "unit not found", ""):
        assert not rc.looks_dropped(fine), fine


def test_an_unexpected_drop_is_retried_with_a_growing_delay_then_recovers():
    c = Clock()
    w = rc.Watch(clock=c)
    assert w.state == "up" and w.recovered() is None and not w.should_retry()
    w.lost()
    assert w.state == "lost" and w.should_retry()
    delays = []
    for _ in range(10):
        delays.append(w.next_delay())
        w.tried()
    assert delays == [2, 3, 5, 8, 10, 15, 20, 30, 30, 30]            # grows, then stays at the last value
    c.t += 75
    away = w.recovered()
    assert away == 75 and w.state == "up" and w.attempts == 0
    w.lost()
    w.lost()                                                          # twice: still the same drop
    assert w.since == c.t


def test_a_reboot_we_asked_for_is_recognised_and_given_time():
    c = Clock()
    w = rc.Watch(clock=c)
    w.expect("reboot")
    c.t += 5
    w.lost()
    assert w.state == "rebooting" and "rebooting" in w.message(8) and "Next try in 8 s" in w.message(8)
    c.t += 800
    assert w.should_retry()                                           # a reboot can take a while
    c.t += 200                                                        # 1005 s > 900 s of patience
    assert not w.should_retry() and w.gave_up and "Couldn't reconnect after 16m" in w.message()
    w.manual()                                                        # the user presses Reconnect: patience restarts
    assert not w.gave_up and w.should_retry()


def test_a_shutdown_is_not_retried_by_itself_and_an_old_expectation_expires():
    c = Clock()
    w = rc.Watch(clock=c)
    w.expect("shutdown")
    w.lost()
    assert w.state == "shutdown" and not w.should_retry() and "won't come back by itself" in w.message()
    w2 = rc.Watch(clock=c)
    w2.expect("reboot")
    c.t += rc.EXPECT_WINDOW + 1                                       # it never dropped: this drop is something else
    w2.lost()
    assert w2.state == "lost" and "Connection lost" in w2.message(3)


def test_messages_and_durations_read_well():
    assert rc.human(7) == "7s" and rc.human(65) == "1m 05s" and rc.human(3725) == "1h 02m" and rc.human(-3) == "0s"
    c = Clock()
    w = rc.Watch(delays=(), clock=c)
    assert w.next_delay() == 5 and w.message() == ""
    w.lost()
    c.t += 12
    assert w.message(4) == "Connection lost 12s ago. Reconnecting … (attempt 1). Next try in 4 s."
