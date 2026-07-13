//! Orchestrator config — LLM provider + downstream service URLs. Port of
//! `orchestrator/bss_orchestrator/config.py`. Env prefix `BSS_`.
//!
//! Only the fields the current slice needs are wired to real consumers; the rest
//! are carried for the OpenRouter client + HTTP tool families that land next.

fn env_or(key: &str, default: &str) -> String {
    std::env::var(key)
        .ok()
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| default.to_string())
}

fn env_int(key: &str, default: i64) -> i64 {
    std::env::var(key)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

/// Resolved orchestrator settings.
#[derive(Debug, Clone)]
pub struct Settings {
    pub llm_base_url: String,
    pub llm_model: String,
    pub llm_api_key: String,
    pub llm_http_referer: String,
    pub llm_app_name: String,
    pub llm_max_tokens: i64,
    pub llm_frequency_penalty: f64,
    pub db_url: String,
    pub env: String,
    pub tenant_default: String,
}

impl Settings {
    pub fn from_env() -> Self {
        let llm_frequency_penalty = std::env::var("BSS_LLM_FREQUENCY_PENALTY")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(0.0);
        Self {
            llm_base_url: env_or("BSS_LLM_BASE_URL", "https://openrouter.ai/api/v1"),
            llm_model: env_or("BSS_LLM_MODEL", "deepseek/deepseek-v4-pro"),
            llm_api_key: env_or("BSS_LLM_API_KEY", ""),
            llm_http_referer: env_or(
                "BSS_LLM_HTTP_REFERER",
                "https://github.com/chiam-ck/bss-cli",
            ),
            llm_app_name: env_or("BSS_LLM_APP_NAME", "bss-cli"),
            llm_max_tokens: env_int("BSS_LLM_MAX_TOKENS", 2048),
            llm_frequency_penalty,
            db_url: env_or("BSS_DB_URL", ""),
            env: env_or("BSS_ENV", "development"),
            tenant_default: env_or("BSS_TENANT_DEFAULT", "DEFAULT"),
        }
    }

    /// `X-BSS-Actor` value for LLM-originated calls — the model slug, so the
    /// audit trail reflects which model acted (Python `settings.llm_actor`).
    pub fn llm_actor(&self) -> String {
        format!("llm-{}", self.llm_model.replace('/', "-"))
    }
}

impl Default for Settings {
    fn default() -> Self {
        Self::from_env()
    }
}
