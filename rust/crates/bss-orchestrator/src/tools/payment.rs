//! Payment read tools — cards on file (COF) + charge attempts (TMF676). Port of
//! the read slice of `orchestrator/bss_orchestrator/tools/payment.py`.
//!
//! Each tool returns the `PaymentClient` response verbatim, so byte-parity follows
//! transitively from the P4 payment service golden diff.
//!
//! The write tools (`register_payment_write_tools`: `add_card` — which also runs the
//! pure sandbox `local_tokenize_card` — `remove_method`, `charge`) live here too.

use std::sync::Arc;

use bss_clients::PaymentClient;
use futures_util::future::FutureExt;
use serde_json::Value;

use super::{map_client_err as map_err, opt_str, req_str, RegisteredTool, ToolError, ToolRegistry};

const DESC_LIST_METHODS: &str = include_str!("desc/payment_list_methods.txt");
const DESC_GET_ATTEMPT: &str = include_str!("desc/payment_get_attempt.txt");
const DESC_LIST_ATTEMPTS: &str = include_str!("desc/payment_list_attempts.txt");
const DESC_ADD_CARD: &str = include_str!("desc/payment_add_card.txt");
const DESC_REMOVE_METHOD: &str = include_str!("desc/payment_remove_method.txt");
const DESC_CHARGE: &str = include_str!("desc/payment_charge.txt");

/// Sandbox client-side tokenizer — a pure port of `local_tokenize_card`. Brand from
/// the BIN, FAIL/DECLINE embedded in the token from the raw PAN text, uuid body.
/// Returns `(card_token, last4, brand)` or a `ValueError` for an invalid PAN.
fn local_tokenize_card(card_number: &str) -> Result<(String, String, String), ToolError> {
    let digits: String = card_number
        .chars()
        .filter(|c| *c != ' ' && *c != '-')
        .collect();
    if !digits.chars().all(|c| c.is_ascii_digit()) || digits.len() < 12 {
        return Err(ToolError::Other {
            kind: "ValueError".to_string(),
            // Python `f"Invalid card number: {card_number!r}"` (single-quoted repr).
            detail: format!("Invalid card number: '{card_number}'"),
        });
    }
    let last4: String = digits[digits.len() - 4..].to_string();
    let bin2: i32 = digits[..2].parse().unwrap_or(0);
    let brand = if digits.starts_with('4') {
        "visa"
    } else if (51..=55).contains(&bin2) {
        "mastercard"
    } else if &digits[..2] == "34" || &digits[..2] == "37" {
        "amex"
    } else {
        "unknown"
    };
    let uid = uuid::Uuid::new_v4();
    let up = card_number.to_uppercase();
    let token = if up.contains("FAIL") {
        format!("tok_FAIL_{uid}")
    } else if up.contains("DECLINE") {
        format!("tok_DECLINE_{uid}")
    } else {
        format!("tok_{uid}")
    };
    Ok((token, last4, brand.to_string()))
}

/// Register the three payment **read** tools, each capturing a clone of `client`.
pub fn register_payment_tools(registry: &mut ToolRegistry, client: PaymentClient) {
    let c = client.clone();
    registry.register(RegisteredTool {
        name: "payment.list_methods".to_string(),
        description: DESC_LIST_METHODS.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let cid = req_str(&args, "customer_id")?;
                c.list_methods(&cid).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client.clone();
    registry.register(RegisteredTool {
        name: "payment.get_attempt".to_string(),
        description: DESC_GET_ATTEMPT.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let id = req_str(&args, "attempt_id")?;
                c.get_payment(&id).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    // payment.list_attempts — optional customer/method filters, limit defaults 20.
    let c = client;
    registry.register(RegisteredTool {
        name: "payment.list_attempts".to_string(),
        description: DESC_LIST_ATTEMPTS.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let customer_id = opt_str(&args, "customer_id");
                let method_id = opt_str(&args, "payment_method_id");
                let limit = args.get("limit").and_then(Value::as_i64).unwrap_or(20);
                c.list_payments(customer_id.as_deref(), method_id.as_deref(), limit)
                    .await
                    .map_err(map_err)
            }
            .boxed()
        }),
    });
}

/// Register the three payment **write** tools. `add_card` runs the sandbox
/// tokenizer then attaches (composite); `remove_method` + `charge` are thin.
/// `remove_method` is destructive (safety-gated at the tool boundary).
pub fn register_payment_write_tools(registry: &mut ToolRegistry, client: PaymentClient) {
    // payment.add_card — tokenize (pure) → create_payment_method (sandbox).
    let c = client.clone();
    registry.register(RegisteredTool {
        name: "payment.add_card".to_string(),
        description: DESC_ADD_CARD.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let customer_id = req_str(&args, "customer_id")?;
                let card_number = req_str(&args, "card_number")?;
                let (token, last4, brand) = local_tokenize_card(&card_number)?;
                c.create_payment_method(&customer_id, &token, &last4, &brand, 12, 2030)
                    .await
                    .map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client.clone();
    registry.register(RegisteredTool {
        name: "payment.remove_method".to_string(),
        description: DESC_REMOVE_METHOD.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let method_id = req_str(&args, "method_id")?;
                c.remove_method(&method_id).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    // payment.charge — `currency` defaults SGD; `amount` is the caller's decimal
    // string (Python `Decimal(amount)` → `str` is a no-op for a canonical value, so
    // it is passed through to the payment service verbatim).
    let c = client;
    registry.register(RegisteredTool {
        name: "payment.charge".to_string(),
        description: DESC_CHARGE.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let customer_id = req_str(&args, "customer_id")?;
                let payment_method_id = req_str(&args, "payment_method_id")?;
                let amount = req_str(&args, "amount")?;
                let purpose = req_str(&args, "purpose")?;
                let currency = opt_str(&args, "currency").unwrap_or_else(|| "SGD".to_string());
                c.charge(
                    &customer_id,
                    &payment_method_id,
                    &amount,
                    &currency,
                    &purpose,
                )
                .await
                .map_err(map_err)
            }
            .boxed()
        }),
    });
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::local_tokenize_card;

    #[test]
    fn tokenizer_detects_brand_and_embeds_outcome() {
        let (tok, last4, brand) = local_tokenize_card("4111 1111 1111 1111").unwrap();
        assert_eq!(brand, "visa");
        assert_eq!(last4, "1111");
        assert!(tok.starts_with("tok_"));

        assert_eq!(
            local_tokenize_card("5500000000000004").unwrap().2,
            "mastercard"
        );
        assert_eq!(local_tokenize_card("340000000000009").unwrap().2, "amex");
        assert_eq!(
            local_tokenize_card("6011000000000004").unwrap().2,
            "unknown"
        );

        // FAIL/DECLINE embed in the token from the raw PAN text (digits still valid).
        assert!(local_tokenize_card("4111111111111111")
            .unwrap()
            .0
            .starts_with("tok_"));

        // Too short / non-digit → the Python-style ValueError observation.
        let err = local_tokenize_card("41111").unwrap_err();
        assert!(err.to_observation().contains("ValueError"));
        assert!(err
            .to_observation()
            .contains("Invalid card number: '41111'"));
    }
}
