# Evaluation results

Generated 2026-11-20 21:40 UTC by `make eval`. These are the numbers from that run. Do not edit by hand.

## Setup

| Item | Value |
|---|---|
| Embedding model | BAAI/bge-small-en-v1.5 (384 dimensions, ONNX, CPU) |
| fastembed | 0.9.1 |
| onnxruntime | 1.32.0 |
| PostgreSQL | 17.12 (Debian 17.12-1.pgdg12+1) |
| pgvector | 0.8.7 |
| psycopg | 3.3.9 |
| Python | 3.12.15 |
| Corpus | 40 document versions, 60 chunks (synthetic) |

## Retrieval quality

48 questions from `eval/questions.json`, asked as a fully authorised user (RESTRICTED, compartments alpha and bravo). Scored per document: the chunk ranking is reduced to document ids, first appearance wins. Each method fetches 20 candidate chunks.

| Mode | recall@1 | recall@3 | recall@5 | MRR |
|---|---|---|---|---|
| keyword | 0.906 | 0.958 | 0.979 | 0.945 |
| vector | 0.885 | 0.979 | 1.000 | 0.942 |
| hybrid | 0.927 | 0.958 | 1.000 | 0.955 |

Questions where hybrid did not find every expected document in the top 5: 0.

## Leak check

44 queries from `eval/adversarial_queries.json`, run for 7 user profiles in 3 modes, top 10 chunks each. A leak is a returned chunk whose source file says the profile may not see it: level too high, a compartment the user lacks, or outside its validity dates.

| Profile | Mode | Queries | Chunks returned | Leaks |
|---|---|---|---|---|
| public | keyword | 44 | 102 | 0 |
| public | vector | 44 | 440 | 0 |
| public | hybrid | 44 | 440 | 0 |
| internal | keyword | 44 | 239 | 0 |
| internal | vector | 44 | 440 | 0 |
| internal | hybrid | 44 | 440 | 0 |
| internal+alpha | keyword | 44 | 268 | 0 |
| internal+alpha | vector | 44 | 440 | 0 |
| internal+alpha | hybrid | 44 | 440 | 0 |
| restricted | keyword | 44 | 255 | 0 |
| restricted | vector | 44 | 440 | 0 |
| restricted | hybrid | 44 | 440 | 0 |
| restricted+alpha | keyword | 44 | 316 | 0 |
| restricted+alpha | vector | 44 | 440 | 0 |
| restricted+alpha | hybrid | 44 | 440 | 0 |
| restricted+bravo | keyword | 44 | 277 | 0 |
| restricted+bravo | vector | 44 | 440 | 0 |
| restricted+bravo | hybrid | 44 | 440 | 0 |
| restricted+alpha+bravo | keyword | 44 | 331 | 0 |
| restricted+alpha+bravo | vector | 44 | 440 | 0 |
| restricted+alpha+bravo | hybrid | 44 | 440 | 0 |

Total: 924 searches, 7948 chunks returned, 0 leaks.

## Read this before quoting the numbers

- The questions and the corpus were written by the same author. That flatters every method.
- 48 questions is a small set. One question moves recall by about 2.1 points, so small gaps between modes mean nothing.
- The corpus is synthetic and tiny. At this size PostgreSQL can scan every row, so these numbers say nothing about the HNSW index at scale.
- The questions were written once and not tuned after seeing results.
