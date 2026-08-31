"""A standing order held back by the gate, and the YES that releases it.

This is the whole point of a standing order, and it was broken in the
quietest possible way: the gate refused correctly, the customer was
asked correctly, the reply was recorded correctly -- and the answer
"Thanks, going ahead with your order now" was simply untrue, because
nothing placed it.

Two halves, and each failed on its own:

  * A soft-cap approval is picked up by the deploy() that asked, which
    is sitting there polling for it. A routine's is not -- the entire
    point is that nobody is running anything -- so nothing consumed it.
  * The buyer console only looked for an open question INSIDE a live
    order run, so the question reached no screen either.
"""

import pytest

import buyer_sms
import routines


@pytest.fixture(autouse=True)
def _clean():
    buyer_sms.reset()
    yield
    buyer_sms.reset()


def test_the_question_carries_the_routine_that_raised_it():
    """Without this the reply has no idea what to place."""
    conversation = buyer_sms.ask_approval(
        agent_id="Jeet's Agent", phone="+919023016845",
        cart_label="1x Chicken Biryani", total_inr=220, soft_cap_inr=250,
        why="the price moved", routine_id="rt-test",
    )
    assert conversation.routine_id == "rt-test"
    assert conversation.as_dict()["routine_id"] == "rt-test"


def test_the_question_carries_its_own_text():
    """The REASON a routine was held back lives in the message and
    nowhere else, so a screen that wants to explain it needs this."""
    conversation = buyer_sms.ask_approval(
        agent_id="Jeet's Agent", phone="+919023016845",
        cart_label="1x Chicken Biryani", total_inr=220, soft_cap_inr=250,
        why="chicken biryani was Rs.187 and is Rs.220 now",
    )
    assert "Rs.220 now" in conversation.as_dict()["question"]


def test_a_yes_actually_places_the_standing_order(monkeypatch):
    placed = {}

    def fake_confirm(routine_id, approved):
        placed["routine_id"] = routine_id
        placed["approved"] = approved
        return {"fired": True}

    monkeypatch.setattr(routines, "confirm_pending", fake_confirm)
    conversation = buyer_sms.ask_approval(
        agent_id="Jeet's Agent", phone="+919023016845",
        cart_label="1x Chicken Biryani", total_inr=220, soft_cap_inr=250,
        routine_id="rt-test",
    )
    code = conversation.code

    result = buyer_sms.record_reply("+919023016845", f"YES {code}")
    assert result["handled"]
    assert placed == {"routine_id": "rt-test", "approved": True}


def test_a_no_cancels_it_rather_than_placing_it(monkeypatch):
    placed = {}
    monkeypatch.setattr(routines, "confirm_pending",
                        lambda routine_id, approved: placed.update(approved=approved))
    conversation = buyer_sms.ask_approval(
        agent_id="Jeet's Agent", phone="+919023016845",
        cart_label="1x Chicken Biryani", total_inr=220, soft_cap_inr=250,
        routine_id="rt-test",
    )
    code = conversation.code
    buyer_sms.record_reply("+919023016845", f"NO {code}")
    assert placed == {"approved": False}


def test_a_soft_cap_approval_is_left_for_the_run_that_asked(monkeypatch):
    """The control. An ordinary over-soft-cap approval has a deploy()
    polling for it, and placing it from here as well would order twice."""
    called = []
    monkeypatch.setattr(routines, "confirm_pending",
                        lambda *a, **k: called.append(1))
    conversation = buyer_sms.ask_approval(
        agent_id="Jeet's Agent", phone="+919023016845",
        cart_label="2x Chicken Biryani", total_inr=440, soft_cap_inr=300,
    )                                   # no routine_id -- not a standing order
    code = conversation.code
    buyer_sms.record_reply("+919023016845", f"YES {code}")
    assert called == [], "a soft-cap approval must not be placed from here"


def test_a_failure_to_place_does_not_claim_it_went_ahead(monkeypatch):
    """Saying "going ahead" when nothing went ahead is the bug this file
    exists for. If placing it raises, say so."""
    def boom(routine_id, approved):
        raise RuntimeError("kitchen unreachable")

    monkeypatch.setattr(routines, "confirm_pending", boom)
    conversation = buyer_sms.ask_approval(
        agent_id="Jeet's Agent", phone="+919023016845",
        cart_label="1x Chicken Biryani", total_inr=220, soft_cap_inr=250,
        routine_id="rt-test",
    )
    code = conversation.code
    message = buyer_sms.record_reply("+919023016845", f"YES {code}")["message"]
    assert "could not be placed" in message
    assert "going ahead" not in message
