-- SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
-- SPDX-License-Identifier: AGPL-3.0-or-later

-- Knowledge graph layer (`entity_graph.v1`).
--
-- Purely additive: three new tables, no change to any existing one. The graph
-- is a derived index over the immutable entity_graph.v1 artifacts, so dropping
-- all three and re-running `pi /graph rebuild` is a supported recovery, not a
-- data loss.
--
-- The GAWD doc specifies Apache AGE for node/edge storage. The running Postgres
-- image (pgvector/pgvector:pg16) does not carry the `age` extension, and the
-- doc's own Decision Narrative §1 records storage as a deliberately late-bound,
-- reversible choice because nothing here is a source of truth. These tables are
-- that reversible choice taken relationally; swapping in AGE later is a new
-- migration plus a rebuild, with no contract change.

CREATE TABLE IF NOT EXISTS workflow_stage_transitions (
  transition_id bigserial PRIMARY KEY,
  workflow_id   text NOT NULL REFERENCES workflow_runs(workflow_id) ON DELETE CASCADE,
  stage         text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS workflow_stage_transitions_workflow_idx
  ON workflow_stage_transitions (workflow_id, transition_id);

CREATE TABLE IF NOT EXISTS graph_nodes (
  node_id                text PRIMARY KEY,
  node_type              text NOT NULL,
  canonical_name         text NOT NULL,
  normalized_name        text NOT NULL,
  aliases_json           jsonb NOT NULL DEFAULT '[]'::jsonb,
  properties_json        jsonb NOT NULL DEFAULT '{}'::jsonb,
  embedding_json         jsonb,
  mention_count          int NOT NULL DEFAULT 0,
  needs_review           boolean NOT NULL DEFAULT false,
  first_seen_artifact_id text NOT NULL DEFAULT '',
  pagerank               double precision,
  degree                 int,
  community_id           int,
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now()
);

-- Resolution scans same-type candidates, so the type/name pair is the hot path.
CREATE INDEX IF NOT EXISTS graph_nodes_type_name_idx
  ON graph_nodes (node_type, normalized_name);
CREATE INDEX IF NOT EXISTS graph_nodes_review_idx
  ON graph_nodes (needs_review) WHERE needs_review;

CREATE TABLE IF NOT EXISTS graph_edges (
  edge_id                  text PRIMARY KEY,
  src_node_id              text NOT NULL REFERENCES graph_nodes(node_id) ON DELETE CASCADE,
  dst_node_id              text NOT NULL REFERENCES graph_nodes(node_id) ON DELETE CASCADE,
  edge_type                text NOT NULL,
  confidence               double precision NOT NULL DEFAULT 0,
  weight                   int NOT NULL DEFAULT 1,
  source_artifact_ids_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  needs_review             boolean NOT NULL DEFAULT false,
  created_at               timestamptz NOT NULL DEFAULT now(),
  updated_at               timestamptz NOT NULL DEFAULT now()
);

-- Neighborhood expansion walks both directions from a seed set.
CREATE INDEX IF NOT EXISTS graph_edges_src_idx ON graph_edges (src_node_id);
CREATE INDEX IF NOT EXISTS graph_edges_dst_idx ON graph_edges (dst_node_id);
CREATE INDEX IF NOT EXISTS graph_edges_review_idx
  ON graph_edges (needs_review) WHERE needs_review;

-- The bridge back to pgvector-world rows. Kept relational deliberately: it
-- joins to embedding_chunks and artifacts, which is what makes GraphRAG a
-- cheap lookup rather than a traversal.
CREATE TABLE IF NOT EXISTS graph_node_mentions (
  mention_id  text PRIMARY KEY,
  node_id     text NOT NULL REFERENCES graph_nodes(node_id) ON DELETE CASCADE,
  artifact_id text NOT NULL REFERENCES artifacts(artifact_id),
  chunk_id    text NOT NULL DEFAULT '',
  snippet     text NOT NULL DEFAULT '',
  created_at  timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT graph_node_mentions_unique UNIQUE (node_id, artifact_id, chunk_id)
);

CREATE INDEX IF NOT EXISTS graph_node_mentions_artifact_idx
  ON graph_node_mentions (artifact_id);
CREATE INDEX IF NOT EXISTS graph_node_mentions_node_idx
  ON graph_node_mentions (node_id);
CREATE INDEX IF NOT EXISTS graph_node_mentions_chunk_idx
  ON graph_node_mentions (chunk_id);
