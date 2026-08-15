-- SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
-- SPDX-License-Identifier: AGPL-3.0-or-later

CREATE TABLE IF NOT EXISTS session_contexts (
  session_id text NOT NULL,
  model_id text NOT NULL,
  active_context_artifact_id text REFERENCES artifacts(artifact_id),
  compacted_summary_artifact_id text REFERENCES artifacts(artifact_id),
  token_count int NOT NULL DEFAULT 0,
  max_window_tokens int,
  export_path text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (session_id, model_id)
);

CREATE INDEX IF NOT EXISTS session_contexts_session_idx
  ON session_contexts (session_id, updated_at);
