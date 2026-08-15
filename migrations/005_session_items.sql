-- SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
-- SPDX-License-Identifier: AGPL-3.0-or-later

CREATE TABLE IF NOT EXISTS session_items (
  item_id text PRIMARY KEY,
  session_id text NOT NULL,
  model_id text NOT NULL,
  turn_id text NOT NULL,
  ordinal int NOT NULL,
  item_type text NOT NULL,
  role text NOT NULL,
  content text NOT NULL,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (turn_id, ordinal)
);

CREATE INDEX IF NOT EXISTS session_items_session_order_idx
  ON session_items (session_id, model_id, created_at, turn_id, ordinal);

ALTER TABLE session_contexts
  ADD COLUMN IF NOT EXISTS snapshot_item_id text REFERENCES session_items(item_id);
