"""Portable candidate artifacts and standard Matplotlib diagnostics."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

from kst.calibration.data import MarketSample, WeatherSample
from kst.calibration.probability import calibrate
from kst.calibration.weather import calibrate_weather
from kst.config import Settings


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def write_job(
    settings: Settings,
    samples: list[MarketSample] | list[WeatherSample],
    sources: list[dict[str, Any]],
    output: Path,
    as_of_ms: int,
    *,
    weather: bool = False,
    acquisition: dict[str, Any] | None = None,
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if output.exists():
        raise ValueError(f"Output directory already exists: {output}")
    if weather:
        overlay, report = calibrate_weather(samples, settings, as_of_ms)  # type: ignore[arg-type]
    else:
        candidate, report = calibrate(samples, settings.calibration_tools, as_of_ms)  # type: ignore[arg-type]
        if (output / "calibration.json").resolve() == settings.pricing.calibration.path.resolve():
            raise ValueError("Choose a candidate directory outside the active calibration path")
    report["sources"] = sources
    if acquisition is not None:
        report["acquisition"] = acquisition
    output.mkdir(parents=True, exist_ok=False)
    if weather:
        (output / "weather.yaml").write_text(yaml.safe_dump(overlay, sort_keys=True), encoding="utf-8")
    else:
        candidate.save(output / "calibration.json")
    _write_json(output / "report.json", report)
    with (output / "samples.jsonl").open("w", encoding="utf-8") as stream:
        for sample in samples:
            stream.write(sample.model_dump_json() + "\n")
    if evidence is not None:
        with (output / "evidence.jsonl").open("w", encoding="utf-8") as stream:
            for row in evidence:
                stream.write(json.dumps(row, allow_nan=False) + "\n")
    # Keep font caches in the artifact directory rather than a sandbox-external home directory.
    os.environ.setdefault("MPLCONFIGDIR", str(output.resolve() / ".matplotlib"))
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if weather:
        rows = [(key, value) for key, value in report["stations"].items() if value["status"] == "fitted"]
        fig, ax = plt.subplots(figsize=(8, max(3, len(rows) * 0.6)), layout="constrained")
        positions = list(range(len(rows)))
        ax.barh(
            [y - 0.18 for y in positions],
            [r["baseline"]["log_loss"] for _, r in rows],
            height=0.36,
            label="Current settings",
        )
        ax.barh(
            [y + 0.18 for y in positions],
            [r["calibrated"]["log_loss"] for _, r in rows],
            height=0.36,
            label="Candidate",
        )
        ax.set_yticks(positions, [key for key, _ in rows])
        ax.set_xlabel("Held-out negative log likelihood (lower is better)")
        ax.set_title("Weather calibration: later days only")
        basename = "weather-validation"
    else:
        fig, ax = plt.subplots(figsize=(6, 5), layout="constrained")
        ax.plot([0, 1], [0, 1], linestyle="--", color="0.6", label="Perfect reliability")
        for name, label in (("baseline", "Original market price"), ("calibrated", "Candidate")):
            bins = [b for b in report["holdout"][name]["reliability"] if b["count"]]
            ax.plot([b["predicted"] for b in bins], [b["observed"] for b in bins], marker="o", label=label)
        ax.set(
            xlim=(0, 1),
            ylim=(0, 1),
            xlabel="Mean predicted YES probability",
            ylabel="Observed YES frequency",
            title="Reliability: held-out events only",
        )
        basename = "reliability"
    if report["synthetic"]:
        fig.suptitle("SYNTHETIC DEMONSTRATION", color="darkred", fontsize=10)
    if weather:
        ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1))
    else:
        ax.legend()
    fig.savefig(output / f"{basename}.svg")
    fig.savefig(output / f"{basename}.png", dpi=150)
    plt.close(fig)
    return {
        "output": str(output.resolve()),
        "synthetic": report["synthetic"],
        "kind": report["kind"],
        "review": str((output / "report.json").resolve()),
        "warnings": report["warnings"],
    }
