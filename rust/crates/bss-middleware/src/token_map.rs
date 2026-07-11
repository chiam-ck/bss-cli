//! TokenMap — named-token loader + validator (v0.9).
//!
//! Faithful port of `packages/bss-middleware/bss_middleware/api_token.py`.
//! Behaviour is pinned by golden vectors captured from the Python oracle
//! (`tests/golden_vectors.json`) — the HMAC hashing and identity derivation must
//! be byte-identical across the language boundary (risk R4).

use std::collections::BTreeMap;

use hmac::{Hmac, Mac};
use sha2::Sha256;
use subtle::ConstantTimeEq;

/// Fixed salt for the in-memory hashed token map. Constant-on-disk — not a
/// secret; its purpose is one-wayness of the in-memory representation so
/// debug-logging the map cannot leak a raw token.
const TOKEN_HASH_SALT: &[u8] = b"bss-cli-token-map-v0.9-fixed-salt";

const SENTINEL: &str = "changeme";
const MIN_LENGTH: usize = 32;
const DEFAULT_IDENTITY: &str = "default";
const DEFAULT_ENV_VAR: &str = "BSS_API_TOKEN";

/// `HMAC-SHA-256(salt, token)` → 32 bytes. Pure / deterministic. Mirrors
/// `_hash_token`.
pub fn hash_token(token: &str) -> [u8; 32] {
    // HMAC accepts a key of any length, so `new_from_slice` is infallible here.
    #[allow(clippy::expect_used)]
    let mut mac = <Hmac<Sha256>>::new_from_slice(TOKEN_HASH_SALT)
        .expect("HMAC-SHA256 accepts any key length");
    mac.update(token.as_bytes());
    mac.finalize().into_bytes().into()
}

/// Derive `service_identity` from an env-var name, mirroring
/// `_identity_from_env_var` and the regex `^BSS_(.+)_API_TOKEN$`:
/// - `BSS_API_TOKEN` → `default` (special-cased v0.3 single token);
/// - `BSS_<NAME>_API_TOKEN` → `<name>` lowercased;
/// - anything else → `None` (Python raises `ValueError`).
pub fn identity_from_env_var(name: &str) -> Option<String> {
    if name == DEFAULT_ENV_VAR {
        return Some(DEFAULT_IDENTITY.to_string());
    }
    // `^BSS_(.+)_API_TOKEN$` with greedy `.+` == strip the fixed prefix and the
    // trailing `_API_TOKEN`; the middle (≥1 char) is the identity, lowercased.
    let inner = name.strip_prefix("BSS_")?.strip_suffix("_API_TOKEN")?;
    if inner.is_empty() {
        return None;
    }
    Some(inner.to_lowercase())
}

/// Raised when a loaded map violates the v0.9 doctrine. Never echoes a raw token.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TokenMapInvalid(pub String);

impl std::fmt::Display for TokenMapInvalid {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::error::Error for TokenMapInvalid {}

/// Immutable hashed-token → service-identity map. Keyed by
/// `HMAC-SHA-256(salt, token)`; the raw token never lives on the struct.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TokenMap {
    /// `(token_hash, identity)` pairs in load order (default first, then named
    /// tokens sorted by env-var name).
    entries: Vec<([u8; 32], String)>,
}

impl TokenMap {
    /// Construct from raw entries (used internally + by the v0.3 single-token
    /// compat path).
    pub fn from_entries(entries: Vec<([u8; 32], String)>) -> Self {
        TokenMap { entries }
    }

    /// v0.3 compat — a single literal token with identity `default`.
    pub fn single(token: &str) -> Self {
        TokenMap {
            entries: vec![(hash_token(token), DEFAULT_IDENTITY.to_string())],
        }
    }

    /// All registered identities, in load order. Diagnostics only.
    pub fn identities(&self) -> Vec<&str> {
        self.entries.iter().map(|(_, i)| i.as_str()).collect()
    }

    /// Number of entries (diagnostics/tests).
    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    /// Resolve `presented` to its identity, or `None`. Constant-time per entry
    /// (`subtle::ConstantTimeEq`); the full map is iterated even on a hit so
    /// timing cannot leak which — if any — entry matched. Mirrors `lookup`.
    pub fn lookup(&self, presented: &str) -> Option<String> {
        if presented.is_empty() {
            return None;
        }
        let h = hash_token(presented);
        let mut matched: Option<String> = None;
        for (stored, identity) in &self.entries {
            if bool::from(h.ct_eq(stored)) {
                matched = Some(identity.clone());
            }
        }
        matched
    }
}

/// Build a `TokenMap` from env vars matching the BSS token pattern. Reads
/// `BSS_API_TOKEN` (identity `default`) plus any non-empty `BSS_<NAME>_API_TOKEN`.
/// Does **not** validate — call [`validate_token_map`] separately. Mirrors
/// `load_token_map_from_env`. `env` is a `BTreeMap` so named tokens load in
/// sorted (deterministic) order, matching Python's explicit `sorted(...)`.
pub fn load_token_map(env: &BTreeMap<String, String>) -> TokenMap {
    let mut entries: Vec<([u8; 32], String)> = Vec::new();

    if let Some(v) = env.get(DEFAULT_ENV_VAR) {
        if !v.is_empty() {
            entries.push((hash_token(v), DEFAULT_IDENTITY.to_string()));
        }
    }
    // BTreeMap iterates keys in sorted order → named tokens are deterministic.
    for (name, value) in env {
        if name == DEFAULT_ENV_VAR || value.is_empty() {
            continue;
        }
        if let Some(identity) = identity_from_env_var(name) {
            entries.push((hash_token(value), identity));
        }
    }
    TokenMap::from_entries(entries)
}

/// Enforce the v0.9 doctrine on a loaded map (mirrors `validate_token_map`).
/// Rules: `default` identity present; identities unique; token hashes unique;
/// every raw token ≥32 chars and not the `changeme` sentinel (checked against
/// `env`, since the map holds only hashes). Errors name the offending env var,
/// never the raw value.
pub fn validate_token_map(
    map: &TokenMap,
    env: &BTreeMap<String, String>,
) -> Result<(), TokenMapInvalid> {
    let identities = map.identities();

    // Rule 1 — default required.
    if !identities.contains(&DEFAULT_IDENTITY) {
        return Err(TokenMapInvalid(format!(
            "{DEFAULT_ENV_VAR} is unset; the '{DEFAULT_IDENTITY}' identity is \
             required (v0.3 single-token behaviour). Generate a token via: \
             openssl rand -hex 32"
        )));
    }

    // Rule 2 — unique identities.
    let mut counts: BTreeMap<&str, usize> = BTreeMap::new();
    for ident in &identities {
        *counts.entry(ident).or_insert(0) += 1;
    }
    let duplicates: Vec<&str> = counts
        .iter()
        .filter(|(_, c)| **c > 1)
        .map(|(i, _)| *i)
        .collect();
    if !duplicates.is_empty() {
        return Err(TokenMapInvalid(format!(
            "duplicate service_identity values in token map: {duplicates:?}. \
             Each BSS_*_API_TOKEN env var must derive a unique identity."
        )));
    }

    // Rule 3 — unique token hashes (two identities sharing a token value).
    let mut by_hash: BTreeMap<[u8; 32], Vec<&str>> = BTreeMap::new();
    for (hash, identity) in &map.entries {
        by_hash.entry(*hash).or_default().push(identity);
    }
    if by_hash.values().any(|idents| idents.len() > 1) {
        return Err(TokenMapInvalid(
            "identities sharing a token value. Each named token must be a \
             distinct random value — sharing one token across surfaces defeats \
             the blast-radius point of named tokens. Regenerate via: \
             openssl rand -hex 32"
                .to_string(),
        ));
    }

    // Rule 4 — length + sentinel against the source env (map holds only hashes).
    let mut candidates: Vec<&str> = Vec::new();
    if env.contains_key(DEFAULT_ENV_VAR) {
        candidates.push(DEFAULT_ENV_VAR);
    }
    for name in env.keys() {
        if name != DEFAULT_ENV_VAR && identity_from_env_var(name).is_some() {
            candidates.push(name);
        }
    }
    for name in candidates {
        let value = match env.get(name) {
            Some(v) if !v.is_empty() => v,
            _ => continue,
        };
        if value == SENTINEL {
            return Err(TokenMapInvalid(format!(
                "{name} is still the .env.example sentinel value \
                 ('{SENTINEL}'); replace it with a real token. \
                 Generate via: openssl rand -hex 32"
            )));
        }
        // Python `len(str)` counts code points, not bytes.
        if value.chars().count() < MIN_LENGTH {
            return Err(TokenMapInvalid(format!(
                "{name} is too short ({} chars; need >={MIN_LENGTH}). \
                 Generate via: openssl rand -hex 32",
                value.chars().count()
            )));
        }
    }

    Ok(())
}

/// Load and validate in one call (mirrors `validate_token_map_present`). Returns
/// the validated map or a `TokenMapInvalid` the caller should treat as fail-fast.
pub fn validate_token_map_present(
    env: &BTreeMap<String, String>,
) -> Result<TokenMap, TokenMapInvalid> {
    let map = load_token_map(env);
    validate_token_map(&map, env)?;
    Ok(map)
}
