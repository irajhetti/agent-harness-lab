# Walkthrough

About 700 words, written to be said out loud.

## What it is

This is the retrieval half of a RAG system. It finds the right passages for a question and stops there. No language model writes an answer. It shows two things. A user only gets passages they are cleared to see. And every passage says where it came from. It was built with AI coding agents under my direction.

## Loading the documents

The corpus is forty short procedures for an invented aerodrome. Each file has a header: an id, a version, an access level, optional compartment tags, and the dates it is valid. The loader refuses a file with no label. Nothing defaults to public.

The loader hashes the whole file with SHA-256, splits the body into chunks of up to eighty words, and embeds each chunk on the CPU with a small open model, bge-small. Each chunk is stored in one PostgreSQL table with its vector, a full-text column, the labels and the hash.

## A query, step by step

One. The caller's level and compartments arrive separately from the question. In a real system they come from the identity provider.

Two. The application opens a read-only transaction as a low-privilege role called rag_reader. It sets the level and compartments as settings that last for that transaction only.

Three. It runs two searches: keyword search with PostgreSQL full-text search, and vector search with cosine distance in pgvector. Each returns its top twenty chunks.

Four. The two lists are merged with reciprocal rank fusion. A chunk scores one over sixty plus its rank in each list, and the scores are added. Only ranks are used, so a keyword score never has to be compared with a cosine score.

Five. The top five come back with document id, version, hash, validity dates, access label and both scores.

## Why the filter runs before ranking

Both searches have the access rule in the WHERE clause. The database applies WHERE before ORDER BY and LIMIT. So a forbidden chunk is never ranked.

The other way round is to fetch the top five and then remove what the user may not see. That has two faults. The user gets fewer results, sometimes none. And any bug in the removal step leaks, because the restricted text has already left the database.

## What row level security adds

The WHERE filter is application code, and application code gets changed. So the same rule is also a policy on the table. For rag_reader, PostgreSQL adds that policy to every SELECT.

A test deletes the filter from the queries. The reader still gets nothing forbidden. The same broken query, run as the table owner, does leak. That shows the test is real.

If the session never sets a level, the policy treats it as PUBLIC. It fails closed. The reader cannot write, cannot alter the policy, and cannot read the table that holds the full source text.

## Provenance

Every result carries the hash of its source file. A test recomputes the hash from the file on disk and from the copy in the database. They must match. Old versions stay in the table with an end date. They are kept but never returned.

## Poisoned documents

Three documents contain text like "ignore your access rules". They are stored and returned as ordinary text. The filter is built only from the caller's profile, so nothing a document says can change it. There is no model in this path to be fooled. Protecting a model that reads these passages is a separate job.

## What could go wrong

One. Row level security trusts the session settings. If the application sets the wrong level, the database believes it. It guards against a forgotten filter, not a compromised application.

Two. A wrong label at loading time is enforced faithfully.

Three. Under row level security, PostgreSQL will not use the full-text index for the reader, because the match operator is not marked leakproof. Sixty rows hide that. A large corpus would not.

Four. The demo database has no passwords and listens on loopback only. On this machine the two roles show the mechanism. They are not a real boundary.

Five. The evaluation is forty-eight questions written by the same author as the corpus. All three modes score near the top, so it cannot tell them apart.
