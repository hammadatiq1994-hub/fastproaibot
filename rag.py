"""
Knowledge-base retrieval (RAG) using ChromaDB + a local embedding model.

Documents live in knowledge_docs/ (plain text / markdown). build_knowledge_base.py
chunks them and writes a persistent Chroma collection. At query time we embed
the user question with the same model and return the most relevant chunks.

Embeddings use ChromaDB's built-in ONNX MiniLM (all-MiniLM-L6-v2) so no paid
embedding API and no PyTorch install are required.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import chromadb
from chromadb.config import Settings
from chromadb.utils import embedding_functions

import config

logger = logging.getLogger("chatbot.rag")

_collection = None

# Conservative chunk size: enough context without overflowing the LLM window.
CHUNK_SIZE = 700
CHUNK_OVERLAP = 120

# Markdown-style section headings. Splitting on these keeps related blocks
# (e.g. "Pricing — Payroll Services" and its table) in their own chunk instead
# of diluting them with neighbouring sections, which improves retrieval.
SECTION_HEADING_RE = re.compile(
    r"^(?:"
    r"pricing\b.*"
    r"|about us"
    r"|contact & location"
    r"|our services"
    r"|general guidance"
    r"|booking / next steps"
    r"|frequently asked questions"
    r"|faq"
    r"|assistant rules\b.*"
    r")$",
    re.IGNORECASE,
)


def _embedding_fn():
    """ONNX MiniLM -- downloaded once into the local Chroma cache."""
    return embedding_functions.DefaultEmbeddingFunction()


def get_collection():
    """Open (or create) the persistent Chroma collection."""
    global _collection
    if _collection is None:
        config.CHROMA_DIR.mkdir(parents=True, exist_ok=True)
        client = chromadb.PersistentClient(
            path=str(config.CHROMA_DIR),
            settings=Settings(anonymized_telemetry=False),
        )
        _collection = client.get_or_create_collection(
            name=config.CHROMA_COLLECTION,
            embedding_function=_embedding_fn(),
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


def _split_sections(text: str) -> List[str]:
    """Split on markdown section headings so each section is chunked on its own."""
    sections: List[str] = []
    current: List[str] = []
    for line in text.split("\n"):
        if (
            SECTION_HEADING_RE.match(line.strip())
            and any(part.strip() for part in current)
        ):
            sections.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append("\n".join(current))
    return [section for section in sections if section.strip()]


def _chunk_block(text: str, chunk_size: int, overlap: int) -> List[str]:
    """Chunk a single section, preferring paragraph / sentence boundaries."""
    if len(text) <= chunk_size:
        return [text] if text.strip() else []

    paragraphs = re.split(r"\n{2,}", text)
    chunks: List[str] = []
    buf = ""

    def flush(piece: str) -> None:
        piece = piece.strip()
        if piece:
            chunks.append(piece)

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        candidate = (buf + "\n\n" + para).strip() if buf else para
        if len(candidate) <= chunk_size:
            buf = candidate
            continue
        if buf:
            flush(buf)
        if len(para) <= chunk_size:
            buf = para
            continue
        sentences = re.split(r"(?<=[.!?])\s+", para)
        buf = ""
        for sent in sentences:
            candidate = (buf + " " + sent).strip() if buf else sent
            if len(candidate) <= chunk_size:
                buf = candidate
            else:
                if buf:
                    flush(buf)
                if len(sent) > chunk_size:
                    step = max(chunk_size - overlap, 1)
                    for i in range(0, len(sent), step):
                        flush(sent[i : i + chunk_size])
                    buf = ""
                else:
                    buf = sent
    if buf:
        flush(buf)
    return chunks


def split_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> List[str]:
    """Split text into overlapping chunks, preferring section then paragraph boundaries."""
    text = re.sub(r"\r\n?", "\n", text).strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    chunks: List[str] = []
    for section in _split_sections(text):
        chunks.extend(_chunk_block(section.strip(), chunk_size, overlap))
    return chunks


def ingest_documents(docs_dir: Optional[Path] = None) -> int:
    """
    Read every .txt / .md file in docs_dir, chunk, embed, and upsert into Chroma.

    Returns the number of chunks stored. Existing collection contents are replaced
    so re-running build_knowledge_base.py always reflects the current documents.
    """
    global _collection
    docs_dir = docs_dir or config.KNOWLEDGE_DOCS_DIR
    docs_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(
        [p for p in docs_dir.iterdir() if p.is_file() and p.suffix.lower() in {".txt", ".md"}]
    )
    if not files:
        logger.warning("No .txt or .md files found in %s", docs_dir)
        return 0

    ids: List[str] = []
    documents: List[str] = []
    metadatas: List[dict] = []

    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        chunks = split_text(text)
        logger.info("  %s -> %d chunk(s)", path.name, len(chunks))
        for i, chunk in enumerate(chunks):
            ids.append(f"{path.stem}::{i}")
            documents.append(chunk)
            metadatas.append({"source": path.name, "chunk": i})

    if not documents:
        return 0

    # Recreate the collection so stale chunks disappear.
    config.CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(
        path=str(config.CHROMA_DIR),
        settings=Settings(anonymized_telemetry=False),
    )
    try:
        client.delete_collection(config.CHROMA_COLLECTION)
    except Exception:
        pass
    _collection = client.get_or_create_collection(
        name=config.CHROMA_COLLECTION,
        embedding_function=_embedding_fn(),
        metadata={"hnsw:space": "cosine"},
    )
    _collection.add(ids=ids, documents=documents, metadatas=metadatas)
    logger.info("Stored %d chunks from %d file(s)", len(documents), len(files))
    return len(documents)


def retrieve(query: str, top_k: Optional[int] = None) -> List[Tuple[str, float, str]]:
    """
    Return (chunk_text, similarity_score, source_filename) for the most relevant chunks.

    Similarity is 1 - cosine_distance. Empty list if the collection is empty.
    """
    top_k = top_k or config.RAG_TOP_K
    collection = get_collection()
    if collection.count() == 0:
        logger.warning("Knowledge base is empty. Run python build_knowledge_base.py")
        return []

    result = collection.query(
        query_texts=[query],
        n_results=min(top_k, collection.count()),
        include=["documents", "distances", "metadatas"],
    )

    docs: Sequence[str] = (result.get("documents") or [[]])[0]
    distances: Sequence[float] = (result.get("distances") or [[]])[0]
    metas: Sequence[dict] = (result.get("metadatas") or [[]])[0]

    hits: List[Tuple[str, float, str]] = []
    for doc, dist, meta in zip(docs, distances, metas):
        score = 1.0 - float(dist)
        if score < config.RAG_MIN_SCORE:
            continue
        source = (meta or {}).get("source", "unknown")
        hits.append((doc, score, source))
    return hits


def format_context(hits: List[Tuple[str, float, str]]) -> str:
    """Build the knowledge-base block injected into the LLM system prompt."""
    if not hits:
        return ""
    parts = []
    for i, (text, score, source) in enumerate(hits, start=1):
        parts.append(f"[Source {i}: {source}]\n{text}")
    return "\n\n".join(parts)


def collection_count() -> int:
    try:
        return get_collection().count()
    except Exception:
        return 0
