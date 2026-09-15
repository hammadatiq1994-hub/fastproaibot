"""
Build (or rebuild) the local ChromaDB knowledge base from files in knowledge_docs/.

Usage
-----
    python build_knowledge_base.py

Drop .txt or .md files into knowledge_docs/, then re-run this script whenever
the website content changes. Existing chunks are replaced so the index stays
in sync with the current documents.

The first run downloads the open-source embedding model (all-MiniLM-L6-v2,
~80 MB) into the local Hugging Face cache. No paid embedding API is used.
"""

from __future__ import annotations

import sys

import config
import rag


def main() -> int:
    logger = config.setup_logging()
    docs_dir = config.KNOWLEDGE_DOCS_DIR
    docs_dir.mkdir(parents=True, exist_ok=True)

    files = [p.name for p in docs_dir.iterdir() if p.is_file() and p.suffix.lower() in {".txt", ".md"}]
    if not files:
        logger.error(
            "No .txt or .md files found in %s. Add your website content there and re-run.",
            docs_dir,
        )
        return 1

    logger.info("Building knowledge base from %d file(s) in %s", len(files), docs_dir)
    for name in files:
        logger.info("  - %s", name)

    count = rag.ingest_documents(docs_dir)
    if count == 0:
        logger.error("No chunks were produced. Check that the files are not empty.")
        return 1

    logger.info("Done. Stored %d chunk(s) in %s", count, config.CHROMA_DIR)
    logger.info("You can now start the API: python app.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
