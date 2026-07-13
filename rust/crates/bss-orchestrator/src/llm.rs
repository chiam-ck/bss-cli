//! OpenRouter chat-model client — the production [`ChatModel`]. Port of
//! `orchestrator/bss_orchestrator/llm.py` (which used `langchain_openai.ChatOpenAI`
//! against OpenRouter's OpenAI-compatible endpoint) as a direct `reqwest` call — no
//! LangChain hop (the same "OpenRouter via the openai SDK, no LiteLLM" doctrine).
//!
//! Temperature defaults to 0.0 (BSS ops are deterministic; the model must not invent
//! values), every completion is capped at `llm_max_tokens`, and `frequency_penalty`
//! is sent only when non-zero — matching the Python factory.
//!
//! Tool schemas: `ToolSpec` carries the name + the byte-identical description (which
//! documents every argument via the ported docstrings). This client sends a
//! permissive `{"type":"object"}` parameter schema per tool — the model reads the
//! description to shape args. Deriving strict per-tool JSON Schemas (D5 / schemars)
//! is a refinement; the fixture-corpus R2 gate runs on [`MockChatModel`], not this
//! client, and the live soak validates real tool-call behaviour.

use serde_json::{json, Value};

use crate::chat_model::{AiTurn, ChatMessage, ChatModel, Role, ToolCall, Usage};
use crate::config::Settings;
use crate::tools::ToolSpec;

/// A [`ChatModel`] bound to OpenRouter + the configured model.
#[derive(Clone)]
pub struct OpenRouterChatModel {
    http: reqwest::Client,
    settings: Settings,
    temperature: f64,
}

impl OpenRouterChatModel {
    /// Build a client from resolved [`Settings`]. Errors when `BSS_LLM_API_KEY` is
    /// empty (the Python factory's `RuntimeError`) or the HTTP client can't build.
    pub fn new(settings: Settings, temperature: f64) -> Result<Self, String> {
        if settings.llm_api_key.is_empty() {
            return Err(
                "BSS_LLM_API_KEY is empty. Set it in the repo-root .env before running \
                 the REPL / chat surface."
                    .to_string(),
            );
        }
        let http = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(120))
            .build()
            .map_err(|e| e.to_string())?;
        Ok(Self {
            http,
            settings,
            temperature,
        })
    }

    /// Build from the environment with the default (deterministic) temperature.
    pub fn from_env() -> Result<Self, String> {
        Self::new(Settings::from_env(), 0.0)
    }

    fn request_body(&self, messages: &[ChatMessage], tools: &[ToolSpec]) -> Value {
        let mut map = serde_json::Map::new();
        map.insert("model".to_string(), json!(self.settings.llm_model));
        map.insert(
            "messages".to_string(),
            json!(messages.iter().map(message_json).collect::<Vec<_>>()),
        );
        map.insert("temperature".to_string(), json!(self.temperature));
        map.insert(
            "max_tokens".to_string(),
            json!(self.settings.llm_max_tokens),
        );
        if !tools.is_empty() {
            let tool_specs: Vec<Value> = tools.iter().map(tool_json).collect();
            map.insert("tools".to_string(), Value::Array(tool_specs));
        }
        if self.settings.llm_frequency_penalty != 0.0 {
            map.insert(
                "frequency_penalty".to_string(),
                json!(self.settings.llm_frequency_penalty),
            );
        }
        Value::Object(map)
    }

    async fn call(&self, body: &Value) -> Result<Value, String> {
        let url = format!(
            "{}/chat/completions",
            self.settings.llm_base_url.trim_end_matches('/')
        );
        let resp = self
            .http
            .post(&url)
            .header(
                "Authorization",
                format!("Bearer {}", self.settings.llm_api_key),
            )
            .header("HTTP-Referer", &self.settings.llm_http_referer)
            .header("X-Title", &self.settings.llm_app_name)
            .json(body)
            .send()
            .await
            .map_err(|e| e.to_string())?;
        let status = resp.status();
        let text = resp.text().await.map_err(|e| e.to_string())?;
        if !status.is_success() {
            return Err(format!("openrouter {status}: {text}"));
        }
        serde_json::from_str(&text).map_err(|e| e.to_string())
    }
}

impl ChatModel for OpenRouterChatModel {
    async fn generate(&mut self, messages: &[ChatMessage], tools: &[ToolSpec]) -> AiTurn {
        let body = self.request_body(messages, tools);
        match self.call(&body).await {
            Ok(resp) => parse_turn(&resp, &self.settings.llm_model),
            Err(e) => {
                // The trait is infallible (matching Python where a raised error is
                // caught by the route); surface a diagnostic final turn + log.
                tracing::error!(error = %e, "openrouter.call_failed");
                AiTurn {
                    content: format!("(model error: {e})"),
                    ..Default::default()
                }
            }
        }
    }
}

/// One `ChatMessage` → the OpenAI/OpenRouter message shape.
fn message_json(m: &ChatMessage) -> Value {
    let role = match m.role {
        Role::System => "system",
        Role::User => "user",
        Role::Assistant => "assistant",
        Role::Tool => "tool",
    };
    let mut map = serde_json::Map::new();
    map.insert("role".to_string(), json!(role));
    map.insert("content".to_string(), json!(m.content));
    if !m.tool_calls.is_empty() {
        let calls: Vec<Value> = m
            .tool_calls
            .iter()
            .map(|tc| {
                json!({
                    "id": tc.id,
                    "type": "function",
                    "function": { "name": tc.name, "arguments": tc.args.to_string() },
                })
            })
            .collect();
        map.insert("tool_calls".to_string(), Value::Array(calls));
    }
    if let Some(id) = &m.tool_call_id {
        map.insert("tool_call_id".to_string(), json!(id));
    }
    if let Some(name) = &m.name {
        map.insert("name".to_string(), json!(name));
    }
    Value::Object(map)
}

/// One `ToolSpec` → an OpenAI function-tool with a permissive parameter schema.
fn tool_json(t: &ToolSpec) -> Value {
    json!({
        "type": "function",
        "function": {
            "name": t.name,
            "description": t.description,
            "parameters": { "type": "object", "additionalProperties": true },
        },
    })
}

/// Parse the completion response into an [`AiTurn`] (content + tool_calls + usage).
fn parse_turn(resp: &Value, model: &str) -> AiTurn {
    let message = resp
        .get("choices")
        .and_then(Value::as_array)
        .and_then(|c| c.first())
        .and_then(|c| c.get("message"));
    let content = message
        .and_then(|m| m.get("content"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let tool_calls = message
        .and_then(|m| m.get("tool_calls"))
        .and_then(Value::as_array)
        .map(|calls| {
            calls
                .iter()
                .filter_map(|tc| {
                    let func = tc.get("function")?;
                    let name = func.get("name").and_then(Value::as_str)?.to_string();
                    // `arguments` is a JSON string; parse it, tolerating empties.
                    let args = func
                        .get("arguments")
                        .and_then(Value::as_str)
                        .filter(|s| !s.trim().is_empty())
                        .and_then(|s| serde_json::from_str(s).ok())
                        .unwrap_or_else(|| json!({}));
                    let id = tc
                        .get("id")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_string();
                    Some(ToolCall { id, name, args })
                })
                .collect()
        })
        .unwrap_or_default();
    let usage = resp.get("usage").map(|u| Usage {
        input_tokens: u.get("prompt_tokens").and_then(Value::as_i64).unwrap_or(0),
        output_tokens: u
            .get("completion_tokens")
            .and_then(Value::as_i64)
            .unwrap_or(0),
        model: model.to_string(),
    });
    AiTurn {
        content,
        tool_calls,
        usage,
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    #[test]
    fn parses_content_tool_calls_and_usage() {
        let resp = json!({
            "choices": [{"message": {
                "content": "ok",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "customer.get", "arguments": "{\"customer_id\": \"CUST-1\"}"}
                }]
            }}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3}
        });
        let turn = parse_turn(&resp, "deepseek/deepseek-v4-pro");
        assert_eq!(turn.content, "ok");
        assert_eq!(turn.tool_calls.len(), 1);
        assert_eq!(turn.tool_calls[0].name, "customer.get");
        assert_eq!(turn.tool_calls[0].args, json!({"customer_id": "CUST-1"}));
        let usage = turn.usage.unwrap();
        assert_eq!(usage.input_tokens, 12);
        assert_eq!(usage.output_tokens, 3);
    }

    #[test]
    fn tolerates_empty_arguments_and_no_tool_calls() {
        let resp = json!({"choices": [{"message": {"content": "just text"}}]});
        let turn = parse_turn(&resp, "m");
        assert_eq!(turn.content, "just text");
        assert!(turn.tool_calls.is_empty());
        assert!(turn.usage.is_none());

        let resp = json!({"choices": [{"message": {"content": "",
            "tool_calls": [{"id": "c", "function": {"name": "clock.now", "arguments": ""}}]}}]});
        let turn = parse_turn(&resp, "m");
        assert_eq!(turn.tool_calls[0].args, json!({}));
    }

    #[test]
    fn request_body_shapes_messages_and_tools() {
        let s = Settings::from_env();
        let model = OpenRouterChatModel {
            http: reqwest::Client::new(),
            settings: s,
            temperature: 0.0,
        };
        let msgs = vec![
            ChatMessage::system("sys"),
            ChatMessage::user("hi"),
            ChatMessage::tool("clock.now", "call_1", "{\"now\":\"...\"}"),
        ];
        let tools = vec![ToolSpec {
            name: "clock.now".to_string(),
            description: "read the clock".to_string(),
        }];
        let body = model.request_body(&msgs, &tools);
        assert_eq!(body["messages"][0]["role"], json!("system"));
        assert_eq!(body["messages"][2]["role"], json!("tool"));
        assert_eq!(body["messages"][2]["tool_call_id"], json!("call_1"));
        assert_eq!(body["tools"][0]["function"]["name"], json!("clock.now"));
        assert_eq!(body["temperature"], json!(0.0));
    }
}
