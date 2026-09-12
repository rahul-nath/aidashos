# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Permanent operator redirect from standalone governed sagas to WorkUnits."""

RETIREMENT_DOC = "docs/completed/governed_saga_door_retirement_gawd.md"


def governed_saga_door_refusal() -> str:
    """The permanent redirect from the removed governed saga execution lane."""

    return (
        "the standalone saga door for governed work is retired "
        f"({RETIREMENT_DOC}). Drive this contract through the WorkUnit lane "
        "instead: agent-ledger compile_design_doc <design doc>, then the "
        "start_work_unit command it prints."
    )
