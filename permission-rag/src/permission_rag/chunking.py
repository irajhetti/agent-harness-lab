"""Split a document body into chunks."""

from __future__ import annotations


def chunk_text(body: str, max_words: int = 80) -> list[str]:
    """Pack whole paragraphs into chunks of at most `max_words` words.

    Paragraphs are kept together where they fit, because a paragraph is usually one idea.
    A single paragraph longer than the limit is cut on word boundaries.
    """
    if max_words < 1:
        raise ValueError("max_words must be at least 1")
    chunks: list[str] = []
    current: list[str] = []
    count = 0
    for paragraph in body.split("\n\n"):
        words = paragraph.split()
        for start in range(0, len(words), max_words):
            piece = words[start : start + max_words]
            if current and count + len(piece) > max_words:
                chunks.append("\n\n".join(current))
                current, count = [], 0
            current.append(" ".join(piece))
            count += len(piece)
    if current:
        chunks.append("\n\n".join(current))
    return chunks
