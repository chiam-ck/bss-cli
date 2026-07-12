//! eSIM provider adapter — v0.15 seam for SM-DP+ integration.
//!
//! Port of `app.domain.esim_provider`. Python uses a `@runtime_checkable`
//! Protocol with three concrete classes; Rust models the closed set as an enum.
//! `sim` is the only fully-working v0.15 variant (a near-no-op that defers timing
//! and fault injection to the worker); `onbglobal` / `esim_access` are accepted at
//! startup so `BSS_ESIM_PROVIDER` can be set ahead of the v0.16+ integration, but
//! every call returns the NDA/credentials pointer error.

/// Result of ordering an eSIM profile from the provider.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EsimOrderResult {
    pub success: bool,
    pub provider_reference: Option<String>,
}

/// The closed set of eSIM provider adapters (the Protocol's implementers).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EsimProvider {
    Sim,
    OneGlobal,
    EsimAccess,
}

/// The v0.16+ pointer message, formatted with the adapter's display name — the
/// substring the Python tests match on (`"1GLOBAL Connect"` / `"eSIM Access"`).
fn v016_pointer(name: &str) -> String {
    format!(
        "v0.16+: {name} adapter requires NDA + production credentials. \
         Set BSS_ESIM_PROVIDER=sim until the integration ships."
    )
}

impl EsimProvider {
    /// Order an eSIM profile. `sim` returns success with no reference; the stubs
    /// return the v0.16+ pointer error (`Err`) on every call.
    pub async fn order_profile(
        &self,
        _iccid: &str,
        _imsi: &str,
        _msisdn: &str,
    ) -> Result<EsimOrderResult, String> {
        match self {
            EsimProvider::Sim => Ok(EsimOrderResult {
                success: true,
                provider_reference: None,
            }),
            EsimProvider::OneGlobal => Err(v016_pointer("1GLOBAL Connect")),
            EsimProvider::EsimAccess => Err(v016_pointer("eSIM Access")),
        }
    }

    /// Release an eSIM profile. `sim` is a no-op; the stubs raise.
    pub async fn release_profile(&self, _iccid: &str) -> Result<(), String> {
        match self {
            EsimProvider::Sim => Ok(()),
            EsimProvider::OneGlobal => Err(v016_pointer("1GLOBAL Connect")),
            EsimProvider::EsimAccess => Err(v016_pointer("eSIM Access")),
        }
    }

    /// Resolve the activation code. `sim` returns a synthetic LPA string.
    pub async fn get_activation_code(&self, iccid: &str) -> Result<String, String> {
        match self {
            EsimProvider::Sim => Ok(format!("LPA:1$rsp.example/{iccid}")),
            EsimProvider::OneGlobal => Err(v016_pointer("1GLOBAL Connect")),
            EsimProvider::EsimAccess => Err(v016_pointer("eSIM Access")),
        }
    }
}

/// Resolve `BSS_ESIM_PROVIDER` to a concrete adapter. `sim` fully works;
/// `onbglobal`/`esim_access` are accepted (raise on first call); unknown names
/// fail fast — port of `select_esim_provider`.
pub fn select_esim_provider(name: &str) -> Result<EsimProvider, String> {
    match name {
        "sim" => Ok(EsimProvider::Sim),
        "onbglobal" => Ok(EsimProvider::OneGlobal),
        "esim_access" => Ok(EsimProvider::EsimAccess),
        other => Err(format!(
            "Unknown BSS_ESIM_PROVIDER={other:?}; expected one of \
             'sim' | 'onbglobal' | 'esim_access'"
        )),
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn sim_order_profile_returns_success_without_provider_reference() {
        let r = EsimProvider::Sim
            .order_profile("89010", "525010", "6591234567")
            .await
            .unwrap();
        assert!(r.success);
        assert_eq!(r.provider_reference, None);
    }

    #[tokio::test]
    async fn sim_release_profile_is_noop() {
        assert_eq!(EsimProvider::Sim.release_profile("89010").await, Ok(()));
    }

    #[tokio::test]
    async fn sim_get_activation_code_returns_synthetic_lpa() {
        let code = EsimProvider::Sim
            .get_activation_code("89010")
            .await
            .unwrap();
        assert!(code.starts_with("LPA:1$"));
        assert!(code.contains("89010"));
    }

    #[tokio::test]
    async fn one_global_stub_raises_on_any_call() {
        let p = EsimProvider::OneGlobal;
        assert!(p
            .order_profile("x", "y", "z")
            .await
            .unwrap_err()
            .contains("1GLOBAL Connect"));
        assert!(p
            .release_profile("x")
            .await
            .unwrap_err()
            .contains("1GLOBAL Connect"));
        assert!(p
            .get_activation_code("x")
            .await
            .unwrap_err()
            .contains("1GLOBAL Connect"));
    }

    #[tokio::test]
    async fn esim_access_stub_raises_on_any_call() {
        let err = EsimProvider::EsimAccess
            .order_profile("x", "y", "z")
            .await
            .unwrap_err();
        assert!(err.contains("eSIM Access"));
    }

    #[test]
    fn select_sim() {
        assert_eq!(select_esim_provider("sim"), Ok(EsimProvider::Sim));
    }

    #[test]
    fn select_stubs_no_raise() {
        assert_eq!(
            select_esim_provider("onbglobal"),
            Ok(EsimProvider::OneGlobal)
        );
        assert_eq!(
            select_esim_provider("esim_access"),
            Ok(EsimProvider::EsimAccess)
        );
    }

    #[test]
    fn select_unknown_fails_fast() {
        assert!(select_esim_provider("singpass")
            .unwrap_err()
            .contains("Unknown BSS_ESIM_PROVIDER"));
    }
}
