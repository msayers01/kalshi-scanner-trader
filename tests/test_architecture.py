"""Enforces the dependency rule (ARCHITECTURE.md §6): pure layers must not import adapters/frameworks."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "kst"

PURE = ["domain", "pricing", "strategies", "risk/checks.py", "risk/limits.py", "scanner/signals.py"]
FORBIDDEN = (
    "kst.kalshi",
    "kst.api",
    "kst.discord_bot",
    "kst.persistence",
    "kst.execution",
    "httpx",
    "fastapi",
    "discord",
    "websockets",
    "sqlalchemy",
)


def _files(entry: str) -> list[Path]:
    p = SRC / entry
    return [p] if p.is_file() else sorted(p.rglob("*.py"))


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def test_pure_layers_have_no_adapter_imports() -> None:
    violations = []
    for entry in PURE:
        for f in _files(entry):
            for mod in _imports(f):
                if any(mod == bad or mod.startswith(bad + ".") for bad in FORBIDDEN):
                    violations.append(f"{f.relative_to(SRC)} imports {mod}")
    assert not violations, "\n".join(violations)


def test_only_oms_and_control_touch_brokers() -> None:
    """CLAUDE.md rule 5: nobody but the OMS/ControlService/composition root may place orders."""
    allowed = {"execution", "app.py", "engine/control.py"}
    offenders = []
    for f in SRC.rglob("*.py"):
        rel = f.relative_to(SRC).as_posix()
        if any(rel == a or rel.startswith(a + "/") for a in allowed):
            continue
        text = f.read_text(encoding="utf-8")
        if "broker.place(" in text or ".create_order(" in text:
            offenders.append(rel)
    assert not offenders, offenders
