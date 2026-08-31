# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The operator's written statement of who may do what, compiled.

``POLICIES.md`` is the authority and this module is how the code reads
it. The shape is the one this repository already trusts for its most important
decisions: a human writes a document, a compiler turns it into a typed immutable
revision, the revision is content-hashed, and everything downstream reads the
revision rather than the prose.

It exists because the thing it replaces could not work. ``policies.py`` decides
whether a tool call is permitted by matching the *tool name* against hardcoded
sets - ``send_email``, ``git_merge``, ``stripe_charge``. No ``Capability`` value
appears in any of them, so every capability check passed regardless of what any
ledger said. The rules were real but they were about a different vocabulary, and
nothing connected the two.

Three properties, each a decision rather than an implementation detail:

- **Deny outranks everything.** A ``Never:`` line beats the compiled plan and
  beats a grant in ``tool_permission_requests``. That is the whole point of
  having a written policy: an agent cannot ask its way past it at runtime.
- **An allowlist is a ceiling, not a grant.** ``May:`` narrows what the compiled
  plan already permitted; it never widens it. Two ceilings intersect, which is
  the only composition that cannot surprise the person who wrote either one.
- **A name nothing answers to is a compile error.** A permission line that
  quietly does nothing is worse than no line, because it reads as protection.

Two vocabularies are accepted on a rule line, and that is the point rather than a
convenience. ``PermissionAction`` is what an operator authorises - ``deploy``,
``dependency_install``, ``merge_to_main`` - and ``Capability`` is what the
runtime enforces - ``publish_deployment``, ``run_command``, ``network_access``.
They are different levels of the same decision, and ``work_units.permissions``
already owns the total translation between them, so this reads it rather than
restating it.

Writing the policy in operator terms matters because the person deciding whether
an agent may install a dependency is not deciding about ``run_command``; they are
deciding about installing dependencies, and a document that makes them translate
is a document that gets translated wrong.

One action can mean several capabilities, which is where the care goes.
``dependency_install`` is a command *and* network egress, and ``run_command`` is
also what ``test_command_execution`` means. So denying the install would silently
stop the tests. Rather than resolve that quietly in either direction, an
expansion that puts one capability on both lists is a compile error naming the
actions responsible - because the operator meant one of two different things and
only they know which.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from .capabilities import Capability, UnknownCapability, parse_capability
from .work_units.permissions import ACTION_CAPABILITIES, PermissionAction

DEFAULT_PRINCIPAL = "default"
"""The section that governs any agent without one of its own."""

# Pinned so an edit to `POLICIES.md` cannot pass unnoticed. A tripwire
# rather than a lock: it cannot tell whether an edit was authorised, only refuse
# to let one through silently. `SCHEMA_CONTENT_HASH` makes the same bargain for
# the database schema, for the same reason.
#
# When it fires, two answers are correct and the point is that somebody chooses:
# the edit was intended, so update this hash in the same commit; or it was not,
# so revert the file.
#
# Recomputed by `policy_document_content_hash()`.
POLICY_CONTENT_HASH = "3933f80f92f266d86adfd8d8c107fdf7553c9c9a5267976e4ef375e5bc88603c"

_SECTION = re.compile(r"^##\s+Principal:\s*(?P<name>.+?)\s*$", re.MULTILINE)
_RULE = re.compile(r"^(?P<verb>May|Never):\s*(?P<names>.+?)\s*$", re.MULTILINE)


class PolicyDocumentError(ValueError):
    """The document says something no policy can mean.

    A parse failure rather than a warning, and raised at load rather than at the
    first spawn, because a policy that cannot be read is not a permissive policy
    - it is an unknown one, and the safe response to an unknown policy is to stop.
    """


@dataclass(frozen=True)
class PrincipalPolicy:
    """What one named agent may and may not do.

    ``allowed`` empty means the document states no allowlist for this principal,
    which is different from an allowlist of nothing: the first defers to the
    compiled plan, the second would refuse everything. An empty ``May:`` line is
    a parse error for that reason.
    """

    principal: str
    allowed: frozenset[Capability] = frozenset()
    denied: frozenset[Capability] = frozenset()

    def permits(self, capability: Capability) -> bool:
        """Whether this principal's own section allows the capability.

        ``allowed`` and ``denied`` are disjoint by construction - the parser
        refuses a document that puts one capability on both lines - so the order
        of these two tests carries no meaning. The denial test is still doing
        work: a section with only a ``Never:`` line has an empty allowlist, and
        without it that section would permit the thing it denies.
        """

        if capability in self.denied:
            return False
        if not self.allowed:
            return True
        return capability in self.allowed


@dataclass(frozen=True)
class CompiledPolicy:
    """The whole document as one immutable value.

    Hashed, so two processes can agree they are enforcing the same policy, and so
    a change is a fact rather than a rumour.
    """

    principals: dict[str, PrincipalPolicy] = field(default_factory=dict)
    content_hash: str = ""

    def for_principal(self, principal: str) -> PrincipalPolicy | None:
        """This principal's section, or the default one, or nothing.

        ``None`` means the document declines to say anything, and the caller
        falls back to the compiled plan. That is what makes the document safe to
        adopt one principal at a time.
        """

        own = self.principals.get(principal)
        if own is not None:
            return own
        return self.principals.get(DEFAULT_PRINCIPAL)

    def permits(self, principal: str, capability: Capability) -> bool:
        policy = self.for_principal(principal)
        return True if policy is None else policy.permits(capability)


_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def policy_document_path() -> Path:
    """Where the written policy lives: the repository root, not `config_dir`.

    Deliberately not configurable. A policy that can be swapped by pointing an
    environment variable at another file is not a policy, and `config_dir` is
    exactly that variable - the golden-path test repoints it at a temp directory
    without meaning to change anybody's permissions.

    Root rather than `configs/` for two reasons: `configs/` is TOML by rule
    (`test_application_config_directory_is_toml_only`), and this is meant to be
    read like `CLAUDE.md` and `AGENTS.md` are, which is why it is prose at all.
    """

    return _REPOSITORY_ROOT / "POLICIES.md"


def parse_policy_document(text: str) -> CompiledPolicy:
    """Turn the written document into the value the gate consults.

    Deliberately a small, dumb parser over headings and two verbs. The document
    is meant to be read by a person deciding what an agent may do, and a format
    that needs a manual is a format that gets skimmed.
    """

    principals: dict[str, PrincipalPolicy] = {}
    matches = list(_SECTION.finditer(text))
    for index, match in enumerate(matches):
        name = match.group("name").strip()
        if name in principals:
            raise PolicyDocumentError(
                f"principal {name!r} is declared twice; one section per principal, "
                "so there is one place to look"
            )
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end() : end]
        allowed: set[Capability] = set()
        denied: set[Capability] = set()
        # Which written name put each capability on a list, so a conflict can
        # name the words the operator actually typed rather than the expansion.
        sources: dict[Capability, set[str]] = {}
        for rule in _RULE.finditer(body):
            names = [item.strip() for item in rule.group("names").split(",") if item.strip()]
            if not names:
                raise PolicyDocumentError(
                    f"principal {name!r} has an empty {rule.group('verb')} line; an empty "
                    "allowlist would refuse everything and an empty denial says nothing, "
                    "so neither is writable by accident"
                )
            for raw in names:
                for capability in _resolve_rule_name(name, raw):
                    target = denied if rule.group("verb") == "Never" else allowed
                    target.add(capability)
                    sources.setdefault(capability, set()).add(raw)
        contested = allowed & denied
        if contested:
            detail = "; ".join(
                f"{capability.value} from {', '.join(sorted(sources[capability]))}"
                for capability in sorted(contested, key=lambda item: item.value)
            )
            raise PolicyDocumentError(
                f"principal {name!r} both allows and denies {detail}. Resolving it "
                "either way would carry out an instruction the operator did not "
                "write, and picking the safe one silently would leave a May line "
                "that does nothing with no sign that it does nothing. Note that one "
                "authored action can mean several capabilities - `dependency_install` "
                "is a command and network egress, and `test_command_execution` is "
                "also a command - so the two lines need not look like they overlap. "
                "Name the capability directly on whichever line you meant."
            )
        principals[name] = PrincipalPolicy(
            principal=name,
            allowed=frozenset(allowed),
            denied=frozenset(denied),
        )
    return CompiledPolicy(principals=principals, content_hash=_hash(text))


def _resolve_rule_name(principal: str, raw: str) -> tuple[Capability, ...]:
    """One written name as the capabilities it means.

    An operator action first, because that is the vocabulary the document is
    written in and the one whose names are unambiguous to a person. A capability
    name is accepted too, for the cases an action has no word for yet.

    An action expanding to nothing - `prepare_isolated_worktrees` needs no
    runtime capability - is refused rather than ignored. A rule line that
    resolves to no permission at all reads as protection and is not, which is the
    same reason an unknown name is an error.
    """

    try:
        action = PermissionAction(raw)
    except ValueError:
        pass
    else:
        capabilities = ACTION_CAPABILITIES[action]
        if not capabilities:
            raise PolicyDocumentError(
                f"principal {principal!r} names the action {raw!r}, which needs no "
                "runtime capability, so permitting or denying it would govern "
                "nothing. Remove the line, or name a capability."
            )
        return capabilities
    try:
        return (parse_capability(raw),)
    except UnknownCapability as exc:
        raise PolicyDocumentError(
            f"principal {principal!r} names {raw!r}, which is neither an operator "
            "action nor a capability; a permission line that does nothing reads "
            "as protection"
        ) from exc


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def policy_document_content_hash() -> str:
    """The hash of the document on disk, to compare against the pin.

    Over the file's bytes rather than the parsed form, for the same reason the
    schema hash is: normalizing means deciding which edits may go unnoticed, and
    that decision having been made implicitly is why a tripwire is needed.
    """

    path = policy_document_path()
    return _hash(path.read_text(encoding="utf-8")) if path.exists() else ""


@cache
def load_policy_document() -> CompiledPolicy:
    """The compiled policy, read once per process.

    An absent document compiles to an empty policy that permits everything, and
    that is deliberate: this is a *further* restriction on the compiled plan, so
    a machine with no `POLICIES.md` behaves exactly as it did before the file
    existed rather than refusing to run.
    """

    path = policy_document_path()
    if not path.exists():
        return CompiledPolicy()
    return parse_policy_document(path.read_text(encoding="utf-8"))


__all__ = [
    "DEFAULT_PRINCIPAL",
    "POLICY_CONTENT_HASH",
    "CompiledPolicy",
    "PolicyDocumentError",
    "PrincipalPolicy",
    "load_policy_document",
    "parse_policy_document",
    "policy_document_content_hash",
    "policy_document_path",
]
