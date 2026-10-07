"""Pure transition engine for validation state-changes."""

from __future__ import annotations

from dataclasses import dataclass

from event_trader.contracts.view_state_change import (
    SystemViewState,
    TransitionAction,
    ViewState,
    ViewStateChange,
)


class ValidationStateMachineError(ValueError):
    """Raised when view-state-change transition inputs violate the deterministic matrix."""


@dataclass(frozen=True, slots=True)
class TransitionResult:
    """Deterministic action output for one state transition."""

    from_state: SystemViewState
    to_state: ViewState
    actions: tuple[TransitionAction, ...]

    @property
    def reflection_triggered(self) -> bool:
        return "trigger_reflection" in self.actions


_TRANSITION_MATRIX: dict[tuple[SystemViewState, ViewState], tuple[TransitionAction, ...]] = {
    ("uninitialized", "flat"): ("record_flat",),
    ("uninitialized", "weak_long"): ("open_view", "open_segment"),
    ("uninitialized", "strong_long"): ("open_view", "open_segment"),
    ("uninitialized", "weak_short"): ("open_view", "open_segment"),
    ("uninitialized", "strong_short"): ("open_view", "open_segment"),
    ("flat", "flat"): ("noop",),
    ("flat", "weak_long"): ("open_view", "open_segment"),
    ("flat", "strong_long"): ("open_view", "open_segment"),
    ("flat", "weak_short"): ("open_view", "open_segment"),
    ("flat", "strong_short"): ("open_view", "open_segment"),
    ("weak_long", "flat"): ("close_view", "trigger_reflection"),
    ("weak_long", "weak_long"): ("noop",),
    ("weak_long", "strong_long"): ("resize_segment",),
    ("weak_long", "weak_short"): ("reverse_view", "trigger_reflection"),
    ("weak_long", "strong_short"): ("reverse_view", "trigger_reflection"),
    ("strong_long", "flat"): ("close_view", "trigger_reflection"),
    ("strong_long", "weak_long"): ("resize_segment",),
    ("strong_long", "strong_long"): ("noop",),
    ("strong_long", "weak_short"): ("reverse_view", "trigger_reflection"),
    ("strong_long", "strong_short"): ("reverse_view", "trigger_reflection"),
    ("weak_short", "flat"): ("close_view", "trigger_reflection"),
    ("weak_short", "weak_long"): ("reverse_view", "trigger_reflection"),
    ("weak_short", "strong_long"): ("reverse_view", "trigger_reflection"),
    ("weak_short", "weak_short"): ("noop",),
    ("weak_short", "strong_short"): ("resize_segment",),
    ("strong_short", "flat"): ("close_view", "trigger_reflection"),
    ("strong_short", "weak_long"): ("reverse_view", "trigger_reflection"),
    ("strong_short", "strong_long"): ("reverse_view", "trigger_reflection"),
    ("strong_short", "weak_short"): ("resize_segment",),
    ("strong_short", "strong_short"): ("noop",),
}


def transition_actions(
    *,
    from_state: SystemViewState,
    to_state: ViewState,
) -> tuple[TransitionAction, ...]:
    """Resolve deterministic transition actions for one state step."""
    actions = _TRANSITION_MATRIX.get((from_state, to_state))
    if actions is None:
        raise ValidationStateMachineError(
            f"Unsupported transition from '{from_state}' to '{to_state}'."
        )
    return actions


def resolve_transition(
    *,
    previous_state_change: ViewStateChange | None,
    next_state_change: ViewStateChange,
) -> TransitionResult:
    """Resolve one transition from the optional prior state-change to the next one."""
    if (
        previous_state_change is not None
        and previous_state_change.target_key != next_state_change.target_key
    ):
        raise ValidationStateMachineError(
            "Transition state-changes must share the same target_key."
        )

    from_state: SystemViewState = (
        "uninitialized" if previous_state_change is None else previous_state_change.state
    )
    if next_state_change.source_kind == "basis_handover":
        if previous_state_change is None:
            raise ValidationStateMachineError(
                "basis_handover requires an existing directional state-change."
            )
        if next_state_change.state != previous_state_change.state:
            raise ValidationStateMachineError(
                "basis_handover must preserve the existing view state."
            )
        return TransitionResult(
            from_state=from_state,
            to_state=next_state_change.state,
            actions=("rollover_view",),
        )
    return TransitionResult(
        from_state=from_state,
        to_state=next_state_change.state,
        actions=transition_actions(from_state=from_state, to_state=next_state_change.state),
    )


def _verify_transition_matrix() -> None:
    all_from_states: tuple[SystemViewState, ...] = (
        "uninitialized",
        "flat",
        "weak_long",
        "strong_long",
        "weak_short",
        "strong_short",
    )
    all_to_states: tuple[ViewState, ...] = (
        "flat",
        "weak_long",
        "strong_long",
        "weak_short",
        "strong_short",
    )
    expected_size = len(all_from_states) * len(all_to_states)
    if len(_TRANSITION_MATRIX) != expected_size:
        raise RuntimeError(
            "Transition matrix must include every from_state/to_state cell "
            f"({len(_TRANSITION_MATRIX)} != {expected_size})."
        )
    for from_state in all_from_states:
        for to_state in all_to_states:
            if (from_state, to_state) not in _TRANSITION_MATRIX:
                raise RuntimeError(
                    f"Transition matrix missing cell ('{from_state}', '{to_state}')."
                )


_verify_transition_matrix()

__all__ = [
    "TransitionResult",
    "ValidationStateMachineError",
    "resolve_transition",
    "transition_actions",
]


