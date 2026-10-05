"""Replay-facing helpers for one historical step over canonical replay inputs."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from event_trader.feeds.manual import adapt_historical_manual_dataset
from event_trader.feeds.payload_mappers import (
    adapt_historical_macro_api,
    adapt_historical_news_stream,
)
from event_trader.replay.runner import ReplayRunner, ReplayRunnerError, ReplayStepReceipt


def inject_historical_news_stream(
    *,
    target_key: str,
    replay_at: datetime,
    stream_payload: Mapping[str, object],
    replay_runner: ReplayRunner,
    labels: list[str] | None = None,
    ts_init: datetime | None = None,
) -> ReplayStepReceipt:
    """Run one historical news-stream replay step through the replay surface."""
    if not isinstance(replay_runner, ReplayRunner):
        raise ReplayRunnerError("replay_runner must be a ReplayRunner instance.")
    try:
        ingress_input = adapt_historical_news_stream(stream_payload)
        return replay_runner.run_step(
            target_key=target_key,
            replay_at=replay_at,
            historical_inputs=(ingress_input,),
            labels=labels,
            ts_init=ts_init,
        )
    except ReplayRunnerError:
        raise
    except Exception as exc:
        raise ReplayRunnerError(str(exc)) from exc


def inject_historical_macro_api(
    *,
    target_key: str,
    replay_at: datetime,
    macro_payload: Mapping[str, object],
    replay_runner: ReplayRunner,
    ts_init: datetime | None = None,
) -> ReplayStepReceipt:
    """Run one historical macro/event replay step through the replay surface."""
    if not isinstance(replay_runner, ReplayRunner):
        raise ReplayRunnerError("replay_runner must be a ReplayRunner instance.")
    try:
        ingress_input = adapt_historical_macro_api(macro_payload)
        return replay_runner.run_step(
            target_key=target_key,
            replay_at=replay_at,
            historical_inputs=(ingress_input,),
            labels=["event:macro_release"],
            ts_init=ts_init,
        )
    except ReplayRunnerError:
        raise
    except Exception as exc:
        raise ReplayRunnerError(str(exc)) from exc


def inject_historical_manual_dataset(
    *,
    target_key: str,
    replay_at: datetime,
    manual_payload: Mapping[str, object],
    replay_runner: ReplayRunner,
    labels: list[str] | None = None,
    ts_init: datetime | None = None,
) -> ReplayStepReceipt:
    """Run one historical manual dataset replay step through the replay surface."""
    if not isinstance(replay_runner, ReplayRunner):
        raise ReplayRunnerError("replay_runner must be a ReplayRunner instance.")
    try:
        ingress_input = adapt_historical_manual_dataset(manual_payload)
        return replay_runner.run_step(
            target_key=target_key,
            replay_at=replay_at,
            historical_inputs=(ingress_input,),
            labels=labels,
            ts_init=ts_init,
        )
    except ReplayRunnerError:
        raise
    except Exception as exc:
        raise ReplayRunnerError(str(exc)) from exc


__all__ = [
    "inject_historical_macro_api",
    "inject_historical_manual_dataset",
    "inject_historical_news_stream",
]
