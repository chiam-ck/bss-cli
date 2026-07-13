//! bss-knowledge: doc-corpus indexer + FTS search backing the v0.20 cockpit
//! knowledge tools. Rust port of `packages/bss-knowledge`.
//!
//! The cockpit's failure mode pre-v0.20 was an operator asking "how do I rotate
//! the cockpit token?" and the LLM confidently paraphrasing an outdated answer.
//! v0.20 closes that loop: `knowledge.search` reads the indexed doc corpus and
//! the LLM cites a section anchor for any answer not derivable from tool output.
//!
//! Doctrine (CLAUDE.md, v0.20+):
//! * `phases/V0_*.md` is intentionally NOT indexed. [`INDEXED_PATHS`] is the
//!   source of truth (doctrine guard 16).
//! * Knowledge tools live in the `operator_cockpit` profile only — customer chat
//!   gets no RAG over operator runbooks.
//! * Reindex is operator-initiated; no file-watcher in the cockpit container.
#![forbid(unsafe_code)]

pub mod chunker;
pub mod indexer;
pub mod paths;
pub mod search;

pub use chunker::{chunk_markdown, Chunk};
pub use indexer::{Indexer, ReindexReport};
pub use paths::{kind_for, kind_rank_weight, INDEXED_PATHS};
pub use search::{get_chunk, search_fts, SearchHit};
