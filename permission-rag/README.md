# permission-rag

A small demonstration of permission-aware retrieval. It is the retrieval layer of a RAG system and nothing more. A user asks a question and gets back the passages they are allowed to see, each with a record of where it came from. No language model writes an answer.

Built with AI coding agents under the owner's direction as a demonstration project.

The corpus is synthetic. It is 40 short procedure documents for an invented aerodrome, "Harrowmere". No real organisation, person or customer appears in it.

## What it shows

- Hybrid search in PostgreSQL: full-text search and pgvector cosine similarity, merged with reciprocal rank fusion.
- Access control inside the database, applied before ranking, in two independent layers.
- Provenance on every result: document id, version, SHA-256 of the source file, source path, validity dates and access label.
- Documents that contain injected instructions are stored and returned as plain data. They cannot change the filter.
- A leak test, a labelled evaluation set and real measured numbers.

## Run it

You need `uv`, rootless `podman` and `make`. No API keys. The first `make seed` downloads the embedding model (about 65 MB) into `.model-cache/`.

```
make up      # start PostgreSQL 17 + pgvector in a container named lab-pgvector, on 127.0.0.1:55432
make seed    # build the schema, chunk and embed the corpus, load it
make test    # 152 tests
make eval    # write eval/results.md
make lint    # ruff and mypy
make down    # remove the container and its data
```

Ask a question as a given user:

```
uv run python -m permission_rag query "How is the drone moved?" --level INTERNAL --compartments alpha
uv run python -m permission_rag query "perimeter patrol routes" --level PUBLIC --mode keyword -k 3
```

`--mode` is `hybrid` (default), `keyword` or `vector`.

## Architecture

```
corpus/*.md  ->  corpus.py (parse header, hash)  ->  chunking.py  ->  embed.py  ->  seed.py
                                                                                     | as rag_owner
                                                                          PostgreSQL: chunks table
                                                                                     | as rag_reader
query + user profile  ->  db.py (set session level)  ->  retrieve.py (two searches, RRF)  ->  results
```

- `access.py` states the rule once in Python: level, compartments, validity dates.
- `sql/schema.sql` holds the tables, the two indexes (GIN for text, HNSW for vectors) and the row level security policy.
- `retrieve.py` holds the two SQL queries and the fusion step.
- `evaluate.py` computes recall and MRR, and runs the leak check.

Access levels are ordered: PUBLIC, INTERNAL, RESTRICTED. A chunk may also carry compartment tags such as `alpha`. A user sees a chunk only if their level is high enough, they hold every compartment on it, and the document is in force today.

## The two enforcement layers

**Layer 1: the SQL filter.** Both search queries carry the access rule in their `WHERE` clause. The database applies `WHERE` before `ORDER BY` and `LIMIT`, so a forbidden chunk is never ranked and never takes a place in the top results. The filter values come from the caller's profile as bound parameters.

**Layer 2: row level security.** The application connects as `rag_reader`, a role with `SELECT` on the chunks table and nothing else. A policy on the table applies the same rule to every query that role runs. It reads the caller's level and compartments from transaction-local session settings. If they are not set, the policy treats the caller as PUBLIC with no compartments.

**Why both.** The filter is application code. It can be dropped in a refactor or missed in a new query. Row level security is in the database and covers every query, including ones nobody has written yet. The filter stays because it is visible in the query, and it also covers any role the policy does not apply to, such as the table owner. With both in place, one mistake is not enough to leak.

The tests check each layer alone. With the filter deleted from the queries, the reader still gets nothing forbidden. With row level security out of the picture (running as the table owner), the filter alone still stops every leak. Removing either layer by hand makes the matching tests fail. `REVIEW-CHECKLIST.md` checks 13 and 14 show how.

## Results

From `eval/results.md`, generated on 2026-11-20 with BAAI/bge-small-en-v1.5 (fastembed 0.9.1), PostgreSQL 17.12 and pgvector 0.8.7.

48 questions, asked as a fully authorised user, scored per document:

| Mode | recall@1 | recall@3 | recall@5 | MRR |
|---|---|---|---|---|
| keyword | 0.906 | 0.958 | 0.979 | 0.945 |
| vector | 0.885 | 0.979 | 1.000 | 0.942 |
| hybrid | 0.927 | 0.958 | 1.000 | 0.955 |

Leak check: 44 ordinary and adversarial queries, 7 user profiles, 3 modes. 924 searches, 7,948 chunks returned, 0 leaks.

How to read this. Hybrid is ahead on recall@1 and MRR and behind vector on recall@3. Each gap is one or two questions out of 48, so it is not evidence that one mode is better. All three score near the top, which means the question set is too easy to separate them. The questions were written once, before any run, and were not changed afterwards.

## Limits

- **Synthetic data.** The documents are short, clean and written by one author. Real documents are neither.
- **Small evaluation set.** 48 questions written by the same author as the corpus. Treat the quality numbers as a working harness, not a finding.
- **No generation step.** Nothing here protects a language model from the injected text in a retrieved passage. That would need its own controls.
- **No GraphRAG**, no reranker, no query rewriting.
- **Single node.** One container, no replication, no backups, no connection pool.
- **Trust auth on loopback.** The database has no passwords. This is acceptable only because the data is synthetic and the port is published on 127.0.0.1 and nowhere else. Any local process can connect as any role, including the owner, so on this machine the two roles show the mechanism and are not a real boundary. A real deployment would use managed credentials, a separate secret for each role, and TLS.
- **The session settings are trusted.** `rag_reader` can set its own level. Row level security protects against a query that forgets the filter. It does not protect against application code that lies about the user. A stronger design maps each clearance to its own database role, or verifies a signed claim inside the database. A test pins this limit so it stays visible.
- **The full-text index is not used under row level security.** PostgreSQL will not run the text match operator ahead of the policy, because the operator is not marked leakproof. So `rag_reader` gets a sequential scan for keyword search. With 60 rows that costs nothing. With millions it would. Possible answers, none built here: a narrowly scoped `SECURITY DEFINER` search function, or separate tables or partitions per clearance. A test pins this too.
- **The HNSW index is not exercised.** At this size the planner scans the table, which is exact. A test only confirms the index is usable. With a large table and a selective filter, an approximate index can return fewer rows than asked for. pgvector's iterative scan setting addresses that. It is not set or tested here.
- **Ranking is `ts_rank`, not BM25.** It has no inverse document frequency.
- **Labels are per document.** There is no paragraph-level marking and no audit log of who asked what.
- **A wrong label is enforced faithfully.** The system trusts the header on each file.
