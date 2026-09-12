# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Closed filesystem and network grants for one native Seatbelt application."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class PathScope(StrEnum):
    EXACT = "literal"
    TREE = "subpath"


@dataclass(frozen=True)
class PathGrant:
    path: Path
    scope: PathScope = PathScope.TREE

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", self.path.resolve())

    def render(self) -> str:
        return f"({self.scope.value} {json.dumps(str(self.path))})"

    def contains(self, candidate: Path) -> bool:
        path = candidate.resolve()
        root = self.path
        return path == root or (self.scope is PathScope.TREE and path.is_relative_to(root))


@dataclass(frozen=True)
class TcpGrant:
    host: str
    port: int

    def __post_init__(self) -> None:
        if self.host not in {"*", "localhost"} or not 0 < self.port < 65536:
            raise ValueError("native network grants require a fixed port and known host class")

    def render(self) -> str:
        return f'(remote tcp "{self.host}:{self.port}")'


@dataclass(frozen=True)
class UnixGrant:
    path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", self.path.resolve())

    def render(self) -> str:
        return f"(literal {json.dumps(str(self.path))})"


type NetworkGrant = TcpGrant | UnixGrant


def _intersection(groups: tuple[tuple[str, ...], ...]) -> str | None:
    if any(not group for group in groups):
        return None
    choices = tuple(
        group[0] if len(group) == 1 else f"(require-any {' '.join(group)})" for group in groups
    )
    if not choices:
        raise ValueError("a containment policy must explicitly declare its grants")
    return choices[0] if len(choices) == 1 else f"(require-all {' '.join(choices)})"


@dataclass(frozen=True)
class SeatbeltPolicy:
    """Each nested grant set is intersected, so a child cannot add authority."""

    reads: tuple[tuple[PathGrant, ...], ...]
    writes: tuple[tuple[PathGrant, ...], ...]
    outbound: tuple[tuple[NetworkGrant, ...], ...]
    forbidden_reads: tuple[PathGrant, ...] = ()
    forbidden_writes: tuple[PathGrant, ...] = ()
    deny_other_network: bool = True
    pty: bool = False
    fixed_process_group: bool = False

    def __post_init__(self) -> None:
        if not self.reads or not self.writes or not self.outbound:
            raise ValueError("every containment dimension requires explicit grant sets")

    def intersect(self, child: SeatbeltPolicy) -> SeatbeltPolicy:
        return SeatbeltPolicy(
            reads=(*self.reads, *child.reads),
            writes=(*self.writes, *child.writes),
            outbound=(*self.outbound, *child.outbound),
            forbidden_reads=(*self.forbidden_reads, *child.forbidden_reads),
            forbidden_writes=(*self.forbidden_writes, *child.forbidden_writes),
            deny_other_network=self.deny_other_network or child.deny_other_network,
            pty=self.pty and child.pty,
            fixed_process_group=self.fixed_process_group or child.fixed_process_group,
        )

    def with_broker(self, socket_path: Path, proxy_path: Path | None = None) -> SeatbeltPolicy:
        """The exact gate socket delegates only the caller's own policy."""
        path = PathGrant(socket_path, PathScope.EXACT)
        proxy = (PathGrant(proxy_path, PathScope.EXACT),) if proxy_path is not None else ()
        return SeatbeltPolicy(
            reads=tuple((*group, path, *proxy) for group in self.reads),
            writes=tuple((*group, path) for group in self.writes),
            outbound=tuple((*group, UnixGrant(socket_path)) for group in self.outbound),
            forbidden_reads=self.forbidden_reads,
            forbidden_writes=self.forbidden_writes,
            deny_other_network=self.deny_other_network,
            pty=self.pty,
            fixed_process_group=self.fixed_process_group,
        )

    def allows_read(self, path: Path) -> bool:
        return not any(grant.contains(path) for grant in self.forbidden_reads) and all(
            any(grant.contains(path) for grant in group) for group in self.reads
        )

    def allows_new_subtree(self, directory: Path) -> bool:
        """Fresh child resources require an entirely readable and writable parent tree."""
        directory = directory.resolve()
        return all(
            any(grant.scope is PathScope.TREE and grant.contains(directory) for grant in group)
            for group in (*self.reads, *self.writes)
        ) and not any(
            grant.contains(directory) or grant.path.is_relative_to(directory)
            for grant in (*self.forbidden_reads, *self.forbidden_writes)
        )

    def render(self) -> str:
        rules = ["(version 1)", "(allow default)", "(deny file-read-data)", "(deny file-write*)"]
        for operation, groups in (("file-read-data", self.reads), ("file-write*", self.writes)):
            condition = _intersection(
                tuple(tuple(grant.render() for grant in group) for group in groups)
            )
            if condition is not None:
                rules.append(f"(allow {operation} {condition})")
        rules.append("(deny network*)" if self.deny_other_network else "(deny network-outbound)")
        network = _intersection(
            tuple(tuple(grant.render() for grant in group) for group in self.outbound)
        )
        if network is not None:
            rules.append(f"(allow network-outbound {network})")
        rules.extend(f"(deny file-read-data {grant.render()})" for grant in self.forbidden_reads)
        rules.extend(f"(deny file-write* {grant.render()})" for grant in self.forbidden_writes)
        if self.pty:
            devices = '(literal "/dev/ptmx") (regex #"^/dev/ttys")'
            rules.extend(
                (
                    "(allow pseudo-tty)",
                    *(
                        f"(allow {operation} {devices})"
                        for operation in ("file-ioctl", "file-write*", "file-read-data")
                    ),
                )
            )
        if self.fixed_process_group:
            rules.append(
                "(deny syscall-unix (syscall-number SYS_setsid SYS_setpgid SYS_posix_spawn))"
            )
        return " ".join(rules)
