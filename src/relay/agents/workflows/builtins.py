"""Load the built-in workflows (config/workflows/*.yaml) and match triggers to events."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from relay.agents.workflows.engine import truthy
from relay.agents.workflows.spec import WorkflowSpec


def load_builtin(config_dir: Path) -> dict[str, WorkflowSpec]:
    out = {}
    for f in sorted((config_dir / "workflows").glob("*.yaml")):
        spec = WorkflowSpec.model_validate(yaml.safe_load(f.read_text(encoding="utf-8")))
        out[spec.name] = spec
    return out


def matches(spec: WorkflowSpec, source: str, event_name: str, event: dict[str, Any]) -> bool:
    want = getattr(spec.on, source, None)
    if not want or want != event_name:
        return False
    return truthy(spec.on.condition, {"event": event}) if spec.on.condition else True
