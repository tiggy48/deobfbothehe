"""
Strict trade state machine.

Every state transition in the bot MUST go through `assert_transition`.
This is the single source of truth for what is allowed to happen next,
so no button handler can be tricked (by a stale UI, a double-click, or a
replayed interaction) into skipping a step.
"""

from enum import Enum


class TradeState(str, Enum):
    CREATED = "CREATED"
    INFORMATION_SUBMITTED = "INFORMATION_SUBMITTED"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    AWAITING_AMOUNT_CONFIRMATION = "AWAITING_AMOUNT_CONFIRMATION"
    ROLES_SELECTED = "ROLES_SELECTED"
    AWAITING_DEPOSIT = "AWAITING_DEPOSIT"
    DEPOSIT_DETECTED = "DEPOSIT_DETECTED"
    LTC_CONFIRMED = "LTC_CONFIRMED"
    TRADE_IN_PROGRESS = "TRADE_IN_PROGRESS"
    RELEASE_REQUESTED = "RELEASE_REQUESTED"
    AWAITING_PAYOUT_ADDRESS = "AWAITING_PAYOUT_ADDRESS"
    PAYOUT_ADDRESS_CONFIRMED = "PAYOUT_ADDRESS_CONFIRMED"
    LTC_SENT = "LTC_SENT"
    COMPLETED = "COMPLETED"

    # Terminal / exceptional states, reachable from most non-terminal states
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    DISPUTED = "DISPUTED"


TERMINAL_STATES = {
    TradeState.COMPLETED,
    TradeState.CANCELLED,
    TradeState.FAILED,
    TradeState.EXPIRED,
}

# States from which staff may always force CANCELLED / DISPUTED, as long as
# the trade has not already reached a terminal state.
STAFF_OVERRIDE_ELIGIBLE = {
    TradeState.CREATED,
    TradeState.INFORMATION_SUBMITTED,
    TradeState.AWAITING_CONFIRMATION,
    TradeState.AWAITING_AMOUNT_CONFIRMATION,
    TradeState.ROLES_SELECTED,
    TradeState.AWAITING_DEPOSIT,
    TradeState.DEPOSIT_DETECTED,
    TradeState.LTC_CONFIRMED,
    TradeState.TRADE_IN_PROGRESS,
    TradeState.RELEASE_REQUESTED,
    TradeState.AWAITING_PAYOUT_ADDRESS,
    TradeState.PAYOUT_ADDRESS_CONFIRMED,
}

# The "happy path" forward edges.
_FORWARD_EDGES = {
    TradeState.CREATED: {TradeState.INFORMATION_SUBMITTED},
    TradeState.INFORMATION_SUBMITTED: {TradeState.AWAITING_CONFIRMATION},
    # "Incorrect" resets back to INFORMATION_SUBMITTED so info can be re-entered.
    TradeState.AWAITING_CONFIRMATION: {TradeState.AWAITING_AMOUNT_CONFIRMATION, TradeState.INFORMATION_SUBMITTED},
    TradeState.AWAITING_AMOUNT_CONFIRMATION: {TradeState.ROLES_SELECTED, TradeState.AWAITING_AMOUNT_CONFIRMATION},
    TradeState.ROLES_SELECTED: {TradeState.AWAITING_DEPOSIT},
    TradeState.AWAITING_DEPOSIT: {TradeState.DEPOSIT_DETECTED},
    TradeState.DEPOSIT_DETECTED: {TradeState.LTC_CONFIRMED, TradeState.AWAITING_DEPOSIT},
    TradeState.LTC_CONFIRMED: {TradeState.TRADE_IN_PROGRESS},
    TradeState.TRADE_IN_PROGRESS: {TradeState.RELEASE_REQUESTED},
    TradeState.RELEASE_REQUESTED: {TradeState.AWAITING_PAYOUT_ADDRESS, TradeState.TRADE_IN_PROGRESS},
    TradeState.AWAITING_PAYOUT_ADDRESS: {TradeState.PAYOUT_ADDRESS_CONFIRMED, TradeState.AWAITING_PAYOUT_ADDRESS},
    TradeState.PAYOUT_ADDRESS_CONFIRMED: {TradeState.LTC_SENT},
    TradeState.LTC_SENT: {TradeState.COMPLETED, TradeState.FAILED},
}

for _state in list(STAFF_OVERRIDE_ELIGIBLE):
    _FORWARD_EDGES.setdefault(_state, set())
    _FORWARD_EDGES[_state] |= {TradeState.CANCELLED, TradeState.DISPUTED, TradeState.EXPIRED}


class InvalidTransitionError(Exception):
    def __init__(self, current: TradeState, target: TradeState):
        super().__init__(f"Illegal trade state transition: {current} -> {target}")
        self.current = current
        self.target = target


def can_transition(current: TradeState, target: TradeState) -> bool:
    if current in TERMINAL_STATES:
        return False
    return target in _FORWARD_EDGES.get(current, set())


def assert_transition(current: TradeState, target: TradeState) -> None:
    if not can_transition(current, target):
        raise InvalidTransitionError(current, target)
