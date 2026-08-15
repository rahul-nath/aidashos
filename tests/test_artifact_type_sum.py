# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What `ArtifactType` buys over the string it replaced.

The split between the four cases is only worth having if it is load-bearing,
so these pin the two things it decides: what may satisfy a milestone's evidence
requirement, and what a stored row deserialises to. The serialisation tests are
the compatibility contract; they are what makes this an internal change rather
than a migration of the ledger and the published API type.
"""

from __future__ import annotations

import pytest

from local_first_agent_os.work_units.events import (
    ArtifactKind,
    ArtifactRecord,
    DiagnosticArtifact,
    DiagnosticArtifactKind,
    RequirableArtifact,
    TraceArtifact,
    TraceArtifactKind,
    UnrecognizedArtifact,
    parse_artifact_type,
)


def _record(artifact_type: object) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_type=artifact_type,  # type: ignore[arg-type]
        uri="workunit://test",
        content_hash="abc123",
    )


@pytest.mark.parametrize("kind", list(ArtifactKind))
def test_every_requirable_kind_can_satisfy_a_requirement(kind: ArtifactKind) -> None:
    assert _record(RequirableArtifact(kind)).satisfies_requirement is True


@pytest.mark.parametrize("kind", list(DiagnosticArtifactKind))
def test_no_diagnostic_kind_can_satisfy_a_requirement(kind: DiagnosticArtifactKind) -> None:
    """The rule the sum type exists to enforce.

    A diagnostic is minted precisely because a run failed. Counting one as
    evidence would let the failure discharge the requirement it failed to meet,
    which is how a blocked milestone would report itself complete.
    """

    assert _record(DiagnosticArtifact(kind)).satisfies_requirement is False


@pytest.mark.parametrize("kind", list(TraceArtifactKind))
def test_no_trace_kind_can_satisfy_a_requirement(kind: TraceArtifactKind) -> None:
    """A trace says how a run went, not that it produced what was asked for.

    Unlike a diagnostic it is minted by successful runs too, so the reason it
    cannot discharge a requirement is different: a document that could require
    one would be demanding a particular execution shape, and a run that reached
    the right answer in a single turn would fail for having little to show.
    """

    assert _record(TraceArtifact(kind)).satisfies_requirement is False


def test_an_unknown_stored_type_cannot_satisfy_a_requirement() -> None:
    assert _record(UnrecognizedArtifact("kind_from_a_later_build")).satisfies_requirement is False


@pytest.mark.parametrize("kind", list(ArtifactKind))
def test_requirable_kinds_parse_back_to_themselves(kind: ArtifactKind) -> None:
    assert parse_artifact_type(kind.value) == RequirableArtifact(kind)


@pytest.mark.parametrize("kind", list(DiagnosticArtifactKind))
def test_diagnostic_kinds_parse_back_to_themselves(kind: DiagnosticArtifactKind) -> None:
    assert parse_artifact_type(kind.value) == DiagnosticArtifact(kind)


@pytest.mark.parametrize("kind", list(TraceArtifactKind))
def test_trace_kinds_parse_back_to_themselves(kind: TraceArtifactKind) -> None:
    assert parse_artifact_type(kind.value) == TraceArtifact(kind)


def test_parsing_is_total_over_arbitrary_stored_strings() -> None:
    """The ledger outlives any one build, so the read path may not crash.

    A retired or renamed kind is still a row someone has to be able to read. A
    reader that raised here would make one old artifact poison every projection
    of the WorkUnit that owns it.
    """

    for raw in ("", "retired_kind", "Source_Patch", "source patch", "a" * 500):
        assert parse_artifact_type(raw) == UnrecognizedArtifact(raw)


@pytest.mark.parametrize(
    "artifact_type",
    [RequirableArtifact(kind) for kind in ArtifactKind]
    + [DiagnosticArtifact(kind) for kind in DiagnosticArtifactKind]
    + [TraceArtifact(kind) for kind in TraceArtifactKind]
    + [UnrecognizedArtifact("retired_kind")],
)
def test_every_case_serialises_to_the_string_the_column_already_holds(
    artifact_type: object,
) -> None:
    """The compatibility claim, stated as a test.

    `work_unit_artifacts.artifact_type` is `TEXT`, the published API type is
    `string`, and `ArtifactKind` values are hashed into every compiled plan. All
    three survive this change only because the payload is byte-identical to what
    a bare string produced, so that is asserted rather than assumed.
    """

    record = _record(artifact_type)
    payload = record.to_payload()
    assert payload["artifact_type"] == artifact_type.value  # type: ignore[attr-defined]
    assert isinstance(payload["artifact_type"], str)
    assert parse_artifact_type(payload["artifact_type"]) == artifact_type
