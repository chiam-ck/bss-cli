"""bss-knowledge: doc corpus indexer + search backing the v0.20 cockpit tools.

The cockpit's failure mode pre-v0.20 was that an operator asks "how do
I rotate the cockpit token?" or "what's the prebaked-KYC env flag?" and
the LLM either confidently paraphrased an outdated answer or admitted
it didn't know. v0.20 closes that loop: the cockpit's `knowledge.search`
tool reads the indexed doc corpus and the LLM cites a section anchor
for any answer not derivable from real tool output.

Public API:

  from bss_knowledge import (
      Indexer, ReindexReport,             # indexer.py
      search_fts, get_chunk, SearchHit,   # search.py
      INDEXED_PATHS, KIND_FOR_PATH,       # paths.py — the allowlist
  )

Doctrine (CLAUDE.md, v0.20+):

* phases/V0_*.md is intentionally NOT indexed — historical build plans
  mislead the LLM. The allowlist in `paths.INDEXED_PATHS` is the source
  of truth.
* Knowledge tools live in the operator_cockpit profile only. Customer
  chat does NOT get RAG over operator runbooks (would leak destructive-
  flow hints + perimeter posture). Enforced by validate_profiles().
* Reindex is operator-initiated (`bss admin knowledge reindex`).
  No file-watcher in the cockpit container.
"""

from bss_knowledge.indexer import Indexer, ReindexReport
from bss_knowledge.paths import INDEXED_PATHS, KIND_FOR_PATH
from bss_knowledge.search import SearchHit, get_chunk, search_fts

__all__ = [
    "Indexer",
    "ReindexReport",
    "search_fts",
    "get_chunk",
    "SearchHit",
    "INDEXED_PATHS",
    "KIND_FOR_PATH",
]
