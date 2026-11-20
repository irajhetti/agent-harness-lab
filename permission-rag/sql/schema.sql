-- Run by rag_owner on every seed. Drops and rebuilds, so seeding is repeatable.
DROP TABLE IF EXISTS chunks, documents CASCADE;

-- One row per document version. Holds the full source text so a hash can be re-checked.
-- rag_reader gets no access to this table at all.
CREATE TABLE documents (
    doc_id      text NOT NULL,
    version     int  NOT NULL,
    title       text NOT NULL,
    source_path text NOT NULL,
    sha256      text NOT NULL,
    source_text text NOT NULL,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (doc_id, version)
);

-- One row per chunk. Provenance and governance columns are copied onto every chunk so the
-- access rule reads one table and needs no join.
CREATE TABLE chunks (
    chunk_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    doc_id       text NOT NULL,
    version      int  NOT NULL,
    chunk_index  int  NOT NULL,
    title        text NOT NULL,
    source_path  text NOT NULL,
    sha256       text NOT NULL,
    access_level text NOT NULL CHECK (access_level IN ('PUBLIC', 'INTERNAL', 'RESTRICTED')),
    -- Any label that is not PUBLIC or INTERNAL ranks as the most restricted.
    access_rank  smallint GENERATED ALWAYS AS (
        CASE access_level WHEN 'PUBLIC' THEN 0 WHEN 'INTERNAL' THEN 1 ELSE 2 END
    ) STORED,
    compartments text[] NOT NULL DEFAULT '{}',
    valid_from   date NOT NULL,
    valid_to     date,
    text         text NOT NULL,
    embedding    vector(384) NOT NULL,
    tsv          tsvector GENERATED ALWAYS AS (to_tsvector('english', title || ' ' || text)) STORED,
    FOREIGN KEY (doc_id, version) REFERENCES documents (doc_id, version),
    UNIQUE (doc_id, version, chunk_index)
);

CREATE INDEX chunks_tsv_gin ON chunks USING gin (tsv);
CREATE INDEX chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops);

-- The caller's clearance, read from session settings. Unset, empty or unknown means PUBLIC
-- with no compartments, so a session that forgets to set them fails closed.
CREATE OR REPLACE FUNCTION app_user_rank() RETURNS int LANGUAGE sql STABLE AS $$
    SELECT CASE current_setting('app.user_level', true)
        WHEN 'RESTRICTED' THEN 2 WHEN 'INTERNAL' THEN 1 ELSE 0 END
$$;

CREATE OR REPLACE FUNCTION app_user_compartments() RETURNS text[] LANGUAGE sql STABLE AS $$
    SELECT string_to_array(coalesce(current_setting('app.user_compartments', true), ''), ',')
$$;

-- Layer 2: row level security. Applies to rag_reader on every SELECT, whatever the query says.
ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;

CREATE POLICY chunks_reader_select ON chunks FOR SELECT TO rag_reader USING (
    access_rank <= app_user_rank()
    AND compartments <@ app_user_compartments()
    AND valid_from <= current_date
    AND (valid_to IS NULL OR valid_to >= current_date)
);

REVOKE ALL ON documents, chunks FROM PUBLIC;
REVOKE ALL ON documents, chunks FROM rag_reader;
GRANT SELECT ON chunks TO rag_reader;
