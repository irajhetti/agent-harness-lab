"""Command line: `python -m permission_rag query "..." --level INTERNAL --compartments alpha`."""

from __future__ import annotations

import argparse
import sys

import psycopg

from .access import User
from .db import reader_session
from .retrieve import MODES, Result, search


def _print_result(position: int, r: Result) -> None:
    tags = f" +{','.join(r.compartments)}" if r.compartments else ""
    keyword = f"#{r.keyword_rank} {r.keyword_score:.4f}" if r.keyword_rank else "-"
    vector = f"#{r.vector_rank} {r.vector_score:.4f}" if r.vector_rank else "-"
    print(f"{position}. {r.doc_id} v{r.version} chunk {r.chunk_index}: {r.title}")
    print(f"   access: {r.access_level}{tags}   valid: {r.valid_from} to {r.valid_to or 'open'}")
    print(f"   source: {r.source_path}")
    print(f"   sha256: {r.sha256}")
    print(f"   scores: rrf {r.rrf_score:.4f} | keyword {keyword} | vector {vector}")
    print(f"   text:   {' '.join(r.text.split())}")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="permission_rag", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    query = commands.add_parser("query", help="retrieve chunks the given user may see")
    query.add_argument("text")
    query.add_argument("--level", default="PUBLIC", help="PUBLIC, INTERNAL or RESTRICTED")
    query.add_argument("--compartments", default="", help="comma-separated, e.g. alpha,bravo")
    query.add_argument("--mode", choices=MODES, default="hybrid")
    query.add_argument("-k", type=int, default=5, help="number of results")
    commands.add_parser("seed", help="rebuild the schema and load the corpus")
    commands.add_parser("eval", help="run the evaluation and write eval/results.md")
    args = parser.parse_args(argv)

    try:
        if args.command == "seed":
            from .seed import seed

            docs, chunks = seed()
            print(f"seeded {docs} document versions, {chunks} chunks")
        elif args.command == "eval":
            from .evaluate import RESULTS_FILE, run_eval

            print(run_eval())
            print(f"written to {RESULTS_FILE}")
        else:
            user = User.parse(args.level, args.compartments)
            with reader_session(user) as cur:
                results = search(cur, user, args.text, args.mode, args.k)
            held = ",".join(sorted(user.compartments)) or "none"
            print(f"user: {user.level.name}, compartments: {held}   mode: {args.mode}\n")
            for position, result in enumerate(results, start=1):
                _print_result(position, result)
            if not results:
                print("no results this user may see")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except psycopg.OperationalError as exc:
        print(
            f"error: database not reachable. Run `make up` and `make seed`.\n{exc}", file=sys.stderr
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
