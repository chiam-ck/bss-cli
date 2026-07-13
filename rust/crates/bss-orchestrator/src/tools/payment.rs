//! Payment read tools — cards on file (COF) + charge attempts (TMF676). Port of
//! the read slice of `orchestrator/bss_orchestrator/tools/payment.py`.
//!
//! Each tool returns the `PaymentClient` response verbatim, so byte-parity follows
//! transitively from the P4 payment service golden diff.
//!
//! The write tools (`add_card` — which also does the sandbox client-side
//! tokenizer, `remove_method`, `charge`) land with the payment write slice.

use std::sync::Arc;

use bss_clients::PaymentClient;
use futures_util::future::FutureExt;
use serde_json::Value;

use super::{map_client_err as map_err, opt_str, req_str, RegisteredTool, ToolRegistry};

const DESC_LIST_METHODS: &str = include_str!("desc/payment_list_methods.txt");
const DESC_GET_ATTEMPT: &str = include_str!("desc/payment_get_attempt.txt");
const DESC_LIST_ATTEMPTS: &str = include_str!("desc/payment_list_attempts.txt");

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
