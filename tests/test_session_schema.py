# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from pathlib import Path

from sqlalchemy import inspect, text

from local_first_agent_os.db import Database
from local_first_agent_os.settings import Settings


def test_create_all_upgrades_existing_session_context_table(tmp_path: Path) -> None:
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'legacy.sqlite3'}")
    database = Database(settings)
    with database.engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE session_contexts (
                    session_id text NOT NULL,
                    model_id text NOT NULL,
                    PRIMARY KEY (session_id, model_id)
                )
                """
            )
        )

    database.create_database_schema()

    columns = {
        column["name"] for column in inspect(database.engine).get_columns("session_contexts")
    }
    assert "snapshot_item_id" in columns
