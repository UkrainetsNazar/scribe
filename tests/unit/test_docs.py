import re
from pathlib import Path

from scribe.domain.recordings import ALLOWED_TRANSITIONS
from scribe.infrastructure.db import models  # noqa: F401  (registers the tables)
from scribe.infrastructure.db.base import Base
from scribe.infrastructure.messaging.topology import QUEUES

DOC = Path(__file__).resolve().parents[2] / "docs" / "architecture.md"


def _diagram(kind: str) -> str:
    match = re.search(rf"```mermaid\n({kind}[^\n]*\n.*?)```", DOC.read_text(), re.S)
    assert match, f"no {kind} diagram in {DOC.name}"
    return match.group(1)


def _er_entities(diagram: str) -> dict[str, set[str]]:
    entities: dict[str, set[str]] = {}
    current: str | None = None
    for line in diagram.splitlines():
        stripped = line.strip()
        if current is None:
            opened = re.fullmatch(r"(\w+) \{", stripped)
            if opened:
                current = opened.group(1)
                entities[current] = set()
        elif stripped == "}":
            current = None
        else:
            entities[current].add(stripped.split()[1])  # "<type> <name> [keys]"
    return entities


def test_er_diagram_matches_the_database_schema() -> None:
    entities = _er_entities(_diagram("erDiagram"))
    assert set(entities) == set(Base.metadata.tables)
    for name, columns in entities.items():
        assert columns == set(Base.metadata.tables[name].columns.keys()), name


def test_state_diagram_matches_the_state_machine() -> None:
    drawn = set(re.findall(r"^\s+(\w+) --> (\w+)", _diagram("stateDiagram-v2"), re.M))
    allowed = {(s.value, t.value) for s, targets in ALLOWED_TRANSITIONS.items() for t in targets}
    assert drawn == allowed


def test_topology_diagram_mentions_every_queue_and_routing_key() -> None:
    text = DOC.read_text()
    for spec in QUEUES:
        assert spec.name in text, spec.name
        for key in spec.routing_keys:
            assert key in text, key
