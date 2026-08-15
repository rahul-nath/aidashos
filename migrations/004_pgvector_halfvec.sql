-- SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
-- SPDX-License-Identifier: AGPL-3.0-or-later

-- 004_pgvector_halfvec.sql
--
-- Finishes the pgvector migration. The embedding_chunks table previously stored
-- vectors in a jsonb column (embedding_json) and left the untyped `embedding
-- vector` column unpopulated, so retrieval fell back to an O(n) Python cosine
-- scan. This makes `embedding` an indexable halfvec(2048) with an HNSW cosine
-- index, enabling sublinear `ORDER BY embedding <=> query` search.
--
-- The application schema is created by SQLAlchemy create_all(); this file is
-- the equivalent DDL and upgrades a pre-existing postgres database in place.
-- Embeddings are Matryoshka-truncated to 2048 dims on write (pgvector's HNSW
-- index caps at 2000 dims for `vector`, 4000 for `halfvec`).

CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE embedding_chunks DROP COLUMN IF EXISTS embedding_json;
ALTER TABLE embedding_chunks DROP COLUMN IF EXISTS embedding;
ALTER TABLE embedding_chunks ADD COLUMN embedding halfvec(2048);

CREATE INDEX IF NOT EXISTS embedding_chunks_embedding_hnsw
  ON embedding_chunks USING hnsw (embedding halfvec_cosine_ops);
