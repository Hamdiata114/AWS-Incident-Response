"""Tests for check_deadline."""

import time

from shared.agent_utils import check_deadline, DEADLINE_BUFFER


def _make_state(deadline_remaining=300):
    return {"deadline": time.time() + deadline_remaining}


class TestCheckDeadline:
    def test_expired(self):
        state = _make_state()
        assert check_deadline(state, now=state["deadline"] + 1) is True

    def test_within_buffer(self):
        state = _make_state()
        assert check_deadline(state, now=state["deadline"] - (DEADLINE_BUFFER - 1)) is True

    def test_plenty_of_time(self):
        state = _make_state()
        assert check_deadline(state, now=state["deadline"] - (DEADLINE_BUFFER + 1)) is False

    def test_exactly_at_buffer(self):
        state = _make_state()
        assert check_deadline(state, now=state["deadline"] - DEADLINE_BUFFER) is False
