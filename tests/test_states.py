import pytest

from tpf.orchestrator.states import Ev, State, TransitionError, apply


def test_happy_path_reaches_delivered():
    s, attempts = State.NEW, 0
    for ev in [Ev.INTAKE_ACCEPTED, Ev.SPEC_READY, Ev.ESTIMATE_APPROVED, Ev.DESIGN_READY, Ev.CODE_READY,
               Ev.CHECKS_PASSED, Ev.STAGING_DEPLOYED, Ev.ACCEPTED, Ev.RELEASED]:
        tr = apply(s, ev, check_attempts=attempts)
        s, attempts = tr.to_state, tr.check_attempts
    assert s is State.DELIVERED


def test_payment_gate_when_required():
    assert apply(State.AWAITING_ESTIMATE, Ev.ESTIMATE_APPROVED, payment_required=True).to_state is State.AWAITING_PAYMENT
    assert apply(State.AWAITING_PAYMENT, Ev.PAYMENT_RECEIVED).to_state is State.DESIGN


def test_failed_checks_loop_then_escalate():
    tr = apply(State.CHECKS, Ev.CHECKS_FAILED, check_attempts=0, max_check_attempts=3)
    assert (tr.to_state, tr.check_attempts) == (State.DEVELOPMENT, 1)
    tr = apply(State.CHECKS, Ev.CHECKS_FAILED, check_attempts=2, max_check_attempts=3)
    assert tr.to_state is State.PAUSED and tr.paused_from is State.DEVELOPMENT and tr.reason


def test_resume_returns_to_paused_stage_with_fresh_attempts():
    tr = apply(State.PAUSED, Ev.RESUME, check_attempts=3, paused_from=State.DEVELOPMENT)
    assert (tr.to_state, tr.check_attempts) == (State.DEVELOPMENT, 0)


def test_client_change_request_resets_attempts():
    tr = apply(State.AWAITING_ACCEPTANCE, Ev.CHANGES_REQUESTED, check_attempts=2)
    assert (tr.to_state, tr.check_attempts) == (State.DEVELOPMENT, 0)


def test_clarification_round_trip():
    assert apply(State.SPEC, Ev.CLARIFICATION_NEEDED).to_state is State.AWAITING_CLIENT_INFO
    assert apply(State.AWAITING_CLIENT_INFO, Ev.CLIENT_REPLIED).to_state is State.SPEC


@pytest.mark.parametrize("state,event", [
    (State.NEW, Ev.CODE_READY),
    (State.AWAITING_ESTIMATE, Ev.ACCEPTED),       # acceptance before the work exists
    (State.DEVELOPMENT, Ev.RELEASED),             # skipping checks and staging
    (State.PAUSED, Ev.CODE_READY),                # a paused order does not move
])
def test_illegal_transitions_are_rejected(state, event):
    with pytest.raises(TransitionError):
        apply(state, event)


def test_terminal_states_are_final():
    for s in (State.DELIVERED, State.CANCELLED):
        with pytest.raises(TransitionError):
            apply(s, Ev.CANCEL)


def test_escalate_from_any_active_state():
    tr = apply(State.STAGING, Ev.ESCALATE)
    assert tr.to_state is State.PAUSED and tr.paused_from is State.STAGING
