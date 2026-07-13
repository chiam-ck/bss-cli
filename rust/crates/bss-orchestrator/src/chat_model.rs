//! Chat-model abstraction + the deterministic fixture player. Port of the
//! LangChain message surface + `orchestrator/bss_orchestrator/llm_mock.py`.
//!
//! The agent loop is generic over [`ChatModel`]; production wires an OpenRouter
//! client (a later P5c slice), tests wire [`MockChatModel`] which answers from a
//! JSON fixture keyed by substring match against the latest user message — the
//! seam that lets transcript specs assert on tool-call shape without a live model.

use serde::Deserialize;
use serde_json::Value;

use crate::tools::ToolSpec;

/// A message in the conversation the model sees.
#[derive(Debug, Clone)]
pub struct ChatMessage {
    pub role: Role,
    pub content: String,
    pub tool_calls: Vec<ToolCall>,
    pub tool_call_id: Option<String>,
    pub name: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Role {
    System,
    User,
    Assistant,
    Tool,
}

impl ChatMessage {
    pub fn system(content: impl Into<String>) -> Self {
        Self::plain(Role::System, content)
    }
    pub fn user(content: impl Into<String>) -> Self {
        Self::plain(Role::User, content)
    }
    pub fn assistant(content: impl Into<String>) -> Self {
        Self::plain(Role::Assistant, content)
    }

    fn plain(role: Role, content: impl Into<String>) -> Self {
        Self {
            role,
            content: content.into(),
            tool_calls: Vec::new(),
            tool_call_id: None,
            name: None,
        }
    }

    /// A tool-result message paired back to a prior assistant tool_call.
    pub fn tool(
        name: impl Into<String>,
        call_id: impl Into<String>,
        content: impl Into<String>,
    ) -> Self {
        Self {
            role: Role::Tool,
            content: content.into(),
            tool_calls: Vec::new(),
            tool_call_id: Some(call_id.into()),
            name: Some(name.into()),
        }
    }
}

/// A tool invocation the model asked for.
#[derive(Debug, Clone, PartialEq)]
pub struct ToolCall {
    pub id: String,
    pub name: String,
    pub args: Value,
}

/// The model's response for one turn.
#[derive(Debug, Clone, Default)]
pub struct AiTurn {
    pub content: String,
    pub tool_calls: Vec<ToolCall>,
    pub usage: Option<Usage>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Usage {
    pub input_tokens: i64,
    pub output_tokens: i64,
    pub model: String,
}

/// The chat-model seam the agent loop drives. A fresh instance is built per turn
/// (matching the Python `build_chat_model()`-per-`build_graph()` shape), so any
/// per-turn step pointer state resets naturally between turns.
pub trait ChatModel {
    /// Produce the next assistant turn given the running message list. `tools`
    /// is the LLM-visible tool surface (the mock ignores it; a real client binds
    /// the schemas).
    fn generate(
        &mut self,
        messages: &[ChatMessage],
        tools: &[ToolSpec],
    ) -> impl std::future::Future<Output = AiTurn> + Send;
}

// ── MockChatModel — fixture player ──────────────────────────────────────────

#[derive(Debug, Deserialize)]
struct FixtureFile {
    #[serde(default)]
    responses: Vec<FixtureResponse>,
}

#[derive(Debug, Deserialize)]
struct FixtureResponse {
    #[serde(default)]
    name: Option<String>,
    #[serde(rename = "match")]
    match_needle: String,
    #[serde(default)]
    steps: Vec<FixtureStep>,
}

#[derive(Debug, Deserialize, Default)]
struct FixtureStep {
    #[serde(default)]
    tool_calls: Vec<FixtureToolCall>,
    #[serde(default)]
    content: String,
}

#[derive(Debug, Deserialize)]
struct FixtureToolCall {
    #[serde(default)]
    id: Option<String>,
    name: String,
    #[serde(default)]
    args: Value,
}

/// A [`ChatModel`] that reads scripted responses from a JSON fixture. Matches on
/// a case-insensitive substring of the *latest* user message, then walks the
/// matched response's `steps` array one entry per turn.
pub struct MockChatModel {
    fixtures: Vec<FixtureResponse>,
    call_count: usize,
    matched: Option<usize>,
    matched_attempted: bool,
}

impl MockChatModel {
    /// Parse a fixture JSON string (`{"responses": [...]}`). Errors on malformed
    /// input — the fixture is a test asset, so a parse failure is a test bug.
    pub fn from_json(json: &str) -> Result<Self, serde_json::Error> {
        let file: FixtureFile = serde_json::from_str(json)?;
        Ok(Self {
            fixtures: file.responses,
            call_count: 0,
            matched: None,
            matched_attempted: false,
        })
    }

    /// The matched fixture's `name` (for logging / assertions), once matched.
    pub fn matched_name(&self) -> Option<&str> {
        self.matched
            .and_then(|i| self.fixtures.get(i))
            .and_then(|r| r.name.as_deref())
    }

    fn next_turn(&mut self, messages: &[ChatMessage]) -> AiTurn {
        if !self.matched_attempted {
            let user_text = latest_user_text(messages);
            self.matched = self.match_fixture(&user_text);
            self.matched_attempted = true;
        }

        let steps: &[FixtureStep] = self
            .matched
            .and_then(|i| self.fixtures.get(i))
            .map(|r| r.steps.as_slice())
            .unwrap_or(&[]);

        if self.call_count >= steps.len() {
            // Out of scripted turns — neutral "done" so the loop breaks cleanly.
            self.call_count += 1;
            return AiTurn {
                content: "(done)".to_string(),
                ..Default::default()
            };
        }

        let step = &steps[self.call_count];
        // Match Python: increment before building ids so `mock_call_{n}_{i}`
        // uses the post-increment counter.
        self.call_count += 1;
        let n = self.call_count;
        let tool_calls: Vec<ToolCall> = step
            .tool_calls
            .iter()
            .enumerate()
            .map(|(i, tc)| ToolCall {
                id: tc
                    .id
                    .clone()
                    .unwrap_or_else(|| format!("mock_call_{n}_{i}")),
                name: tc.name.clone(),
                args: tc.args.clone(),
            })
            .collect();
        AiTurn {
            content: step.content.clone(),
            tool_calls,
            usage: None,
        }
    }

    fn match_fixture(&self, user_text: &str) -> Option<usize> {
        if user_text.is_empty() {
            return None;
        }
        let text_lower = user_text.to_lowercase();
        self.fixtures.iter().position(|r| {
            !r.match_needle.is_empty() && text_lower.contains(&r.match_needle.to_lowercase())
        })
    }
}

impl ChatModel for MockChatModel {
    async fn generate(&mut self, messages: &[ChatMessage], _tools: &[ToolSpec]) -> AiTurn {
        self.next_turn(messages)
    }
}

/// The latest user message's content (Python `_latest_user_text`).
fn latest_user_text(messages: &[ChatMessage]) -> String {
    messages
        .iter()
        .rev()
        .find(|m| m.role == Role::User)
        .map(|m| m.content.clone())
        .unwrap_or_default()
}
