"""File-backed writer for counterfactual evaluation artifacts."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from event_trader.storage import WorkspaceLayout

from .contracts import (
    CounterfactualContractError,
    CounterfactualEvaluationReport,
    parse_counterfactual_evaluation_report,
)


class CounterfactualReportWriterError(ValueError):
    """Raised when counterfactual report persistence is invalid."""


class CounterfactualReportWriter:
    """Persist reports under runtime/evaluation without touching agent memory."""

    def __init__(self, layout: WorkspaceLayout) -> None:
        if not isinstance(layout, WorkspaceLayout):
            raise CounterfactualReportWriterError(
                "layout must be a WorkspaceLayout instance."
            )
        self._layout = layout

    def write(self, report: CounterfactualEvaluationReport) -> Path:
        if not isinstance(report, CounterfactualEvaluationReport):
            raise CounterfactualReportWriterError(
                "report must be a CounterfactualEvaluationReport."
            )
        _require_complete_report(report)
        path = self.path_for(report)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(report.to_json_payload(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            raise CounterfactualReportWriterError(
                f"Failed to write counterfactual report {path}: {exc}"
            ) from exc
        return path

    def read_for_episode(
        self,
        target_key: str,
        episode_id: str,
    ) -> CounterfactualEvaluationReport:
        path = self.path_for_episode(target_key=target_key, episode_id=episode_id)
        if not path.exists() or not path.is_file():
            raise CounterfactualReportWriterError(
                f"Missing counterfactual report: {path}"
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CounterfactualReportWriterError(
                f"Failed to read counterfactual report {path}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise CounterfactualReportWriterError(
                f"Counterfactual report payload must be an object: {path}"
            )
        try:
            report = parse_counterfactual_evaluation_report(payload)
        except CounterfactualContractError as exc:
            raise CounterfactualReportWriterError(str(exc)) from exc
        if report.target_key != target_key or report.episode_id != episode_id:
            raise CounterfactualReportWriterError(
                "counterfactual report target_key/episode_id mismatch."
            )
        _require_complete_report(report)
        return report

    def path_for(self, report: CounterfactualEvaluationReport) -> Path:
        if not isinstance(report, CounterfactualEvaluationReport):
            raise CounterfactualReportWriterError(
                "report must be a CounterfactualEvaluationReport."
            )
        return self.path_for_episode(
            target_key=report.target_key,
            episode_id=report.episode_id,
        )

    def path_for_episode(self, *, target_key: str, episode_id: str) -> Path:
        return (
            self._layout.runtime_root
            / "evaluation"
            / "counterfactuals"
            / target_key
            / f"{_artifact_name(episode_id)}.json"
        ).resolve(strict=False)


def _artifact_name(episode_id: str) -> str:
    digest = sha256(episode_id.encode("utf-8")).hexdigest()
    safe_prefix = "".join(
        char if char.isalnum() or char in {"-", "_"} else "_"
        for char in episode_id
    ).strip("_")
    if not safe_prefix:
        return digest
    return f"{safe_prefix[:80]}_{digest[:12]}"


def _require_complete_report(report: CounterfactualEvaluationReport) -> None:
    if report.episode_metrics is None:
        raise CounterfactualReportWriterError(
            "complete counterfactual reports require episode_metrics."
        )
    if report.decision_quality_attribution is None:
        raise CounterfactualReportWriterError(
            "complete counterfactual reports require decision_quality_attribution."
        )


__all__ = ["CounterfactualReportWriter", "CounterfactualReportWriterError"]
