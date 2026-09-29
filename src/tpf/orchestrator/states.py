"""The order lifecycle as a pure state machine: no I/O, fully unit-testable.

Two decisions belong to the client (estimate, acceptance); everything else is advanced by agents
and automatic gates. Failed checks loop back to development; after MAX_CHECK_ATTEMPTS the order
pauses and an engineer is called (escalation).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class State(StrEnum):
    NEW = "new"
    SPEC = "spec"                                    # Analyst writes the spec and estimate
    AWAITING_CLIENT_INFO = "awaiting_client_info"    # Analyst asked the client questions
    AWAITING_ESTIMATE = "awaiting_estimate"          # client decision #1
    AWAITING_PAYMENT = "awaiting_payment"            # prepayment, when required
    DESIGN = "design"                                # Architect
    DEVELOPMENT = "development"                      # Developer
    CHECKS = "checks"                                # QA and security gates
    STAGING = "staging"                              # DevOps: preview deployment
    AWAITING_ACCEPTANCE = "awaiting_acceptance"      # client decision #2
    AWAITING_FINAL_PAYMENT = "awaiting_final_payment"  # remaining balance, before release
    RELEASE = "release"                              # DevOps: production deployment
    DELIVERED = "delivered"
    PAUSED = "paused"                                # escalated to an engineer
    CANCELLED = "cancelled"


TERMINAL = {State.DELIVERED, State.CANCELLED}
WAITING_FOR_CLIENT = {
    State.AWAITING_CLIENT_INFO, State.AWAITING_ESTIMATE, State.AWAITING_PAYMENT,
    State.AWAITING_ACCEPTANCE, State.AWAITING_FINAL_PAYMENT,
}

# Entering one of these states queues the agent task that does its work.
TASK_FOR_STATE: dict[State, str] = {
    State.SPEC: "analyst",
    State.DESIGN: "architect",
    State.DEVELOPMENT: "developer",
    State.CHECKS: "qa",
    State.STAGING: "deploy_staging",
    State.RELEASE: "deploy_production",
    State.AWAITING_PAYMENT: "request_prepayment",           # creates the checkout link, sends it
    State.AWAITING_FINAL_PAYMENT: "request_final_payment",
}


class Ev(StrEnum):
    INTAKE_ACCEPTED = "intake_accepted"
    CLARIFICATION_NEEDED = "clarification_needed"
    CLIENT_REPLIED = "client_replied"
    SPEC_READY = "spec_ready"
    ESTIMATE_APPROVED = "estimate_approved"
    ESTIMATE_REJECTED = "estimate_rejected"
    PAYMENT_RECEIVED = "payment_received"
    FINAL_PAYMENT_RECEIVED = "final_payment_received"
    DESIGN_READY = "design_ready"
    CODE_READY = "code_ready"
    CHECKS_PASSED = "checks_passed"
    CHECKS_FAILED = "checks_failed"
    STAGING_DEPLOYED = "staging_deployed"
    ACCEPTED = "accepted"
    CHANGES_REQUESTED = "changes_requested"
    RELEASED = "released"
    ESCALATE = "escalate"
    RESUME = "resume"
    CANCEL = "cancel"


# Plain transitions: (from, event) -> to.
_TABLE: dict[tuple[State, Ev], State] = {
    (State.NEW, Ev.INTAKE_ACCEPTED): State.SPEC,
    (State.SPEC, Ev.CLARIFICATION_NEEDED): State.AWAITING_CLIENT_INFO,
    (State.AWAITING_CLIENT_INFO, Ev.CLIENT_REPLIED): State.SPEC,
    (State.SPEC, Ev.SPEC_READY): State.AWAITING_ESTIMATE,
    (State.AWAITING_ESTIMATE, Ev.ESTIMATE_REJECTED): State.SPEC,
    (State.AWAITING_PAYMENT, Ev.PAYMENT_RECEIVED): State.DESIGN,
    (State.DESIGN, Ev.DESIGN_READY): State.DEVELOPMENT,
    (State.DEVELOPMENT, Ev.CODE_READY): State.CHECKS,
    (State.CHECKS, Ev.CHECKS_PASSED): State.STAGING,
    (State.STAGING, Ev.STAGING_DEPLOYED): State.AWAITING_ACCEPTANCE,
    (State.AWAITING_FINAL_PAYMENT, Ev.FINAL_PAYMENT_RECEIVED): State.RELEASE,
    (State.AWAITING_ACCEPTANCE, Ev.CHANGES_REQUESTED): State.DEVELOPMENT,
    (State.RELEASE, Ev.RELEASED): State.DELIVERED,
}


class TransitionError(ValueError):
    pass


@dataclass(frozen=True)
class Transition:
    to_state: State
    check_attempts: int
    paused_from: State | None = None
    reason: str | None = None


def apply(
    state: State,
    event: Ev,
    *,
    check_attempts: int = 0,
    max_check_attempts: int = 3,
    payment_required: bool = False,
    final_payment_required: bool = False,
    paused_from: State | None = None,
) -> Transition:
    """Return the transition for `event` in `state`, or raise TransitionError."""
    if state in TERMINAL:
        raise TransitionError(f"order is {state}; no further transitions")

    if event is Ev.CANCEL:
        return Transition(State.CANCELLED, check_attempts)

    if event is Ev.ESCALATE:
        if state is State.PAUSED:
            raise TransitionError("order is already paused")
        return Transition(State.PAUSED, check_attempts, paused_from=state)

    if event is Ev.RESUME:
        if state is not State.PAUSED or paused_from is None:
            raise TransitionError("only a paused order can be resumed")
        # A human looked at it: give the failing stage a fresh set of attempts.
        return Transition(paused_from, 0)

    if state is State.PAUSED:
        raise TransitionError(f"order is paused; '{event}' is not allowed until it is resumed")

    if (state, event) == (State.AWAITING_ESTIMATE, Ev.ESTIMATE_APPROVED):
        target = State.AWAITING_PAYMENT if payment_required else State.DESIGN
        return Transition(target, check_attempts)

    if (state, event) == (State.AWAITING_ACCEPTANCE, Ev.ACCEPTED):
        target = State.AWAITING_FINAL_PAYMENT if final_payment_required else State.RELEASE
        return Transition(target, check_attempts)

    if (state, event) == (State.CHECKS, Ev.CHECKS_FAILED):
        attempts = check_attempts + 1
        if attempts >= max_check_attempts:
            return Transition(
                State.PAUSED, attempts, paused_from=State.DEVELOPMENT,
                reason=f"checks failed {attempts} times",
            )
        return Transition(State.DEVELOPMENT, attempts)

    if (state, event) == (State.AWAITING_ACCEPTANCE, Ev.CHANGES_REQUESTED):
        return Transition(State.DEVELOPMENT, 0)

    try:
        return Transition(_TABLE[(state, event)], check_attempts)
    except KeyError:
        raise TransitionError(f"event '{event}' is not allowed in state '{state}'") from None
