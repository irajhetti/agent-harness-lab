"""Load the synthetic corpus: Markdown files with a small `key: value` header."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .access import Level, parse_compartments

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = PROJECT_ROOT / "corpus"


@dataclass(frozen=True)
class Document:
    doc_id: str
    version: int
    title: str
    level: Level
    compartments: frozenset[str]
    valid_from: date
    valid_to: date | None
    source_path: str  # relative to the project root
    sha256: str  # of the whole source file, header included
    source_text: str
    body: str


def parse_document(path: Path, root: Path = PROJECT_ROOT) -> Document:
    """Parse one file. A missing or unknown label raises: nothing defaults to PUBLIC."""
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    if not text.startswith("---\n") or "\n---\n" not in text:
        raise ValueError(f"{path}: no metadata header")
    header, body = text[4:].split("\n---\n", 1)
    meta = {}
    for line in header.splitlines():
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    try:
        return Document(
            doc_id=meta["id"],
            version=int(meta["version"]),
            title=meta["title"],
            level=Level[meta["access"]],
            compartments=parse_compartments(meta["compartments"]),
            valid_from=date.fromisoformat(meta["valid_from"]),
            valid_to=date.fromisoformat(meta["valid_to"]) if meta["valid_to"] else None,
            source_path=path.resolve().relative_to(root).as_posix(),
            # The hash covers the header too, so changing a label changes the hash.
            sha256=hashlib.sha256(raw).hexdigest(),
            source_text=text,
            body=body.strip(),
        )
    except (KeyError, ValueError) as exc:
        raise ValueError(f"{path}: bad or missing metadata ({exc})") from exc


def load_corpus(directory: Path = CORPUS_DIR) -> list[Document]:
    docs = [parse_document(p) for p in sorted(directory.glob("*.md"))]
    keys = [(d.doc_id, d.version) for d in docs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate (id, version) in corpus")
    return docs
