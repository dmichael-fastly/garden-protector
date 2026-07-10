"""Shared identity + token-name contracts.

These values are GENERATED from contract/spec.toml into provision/contract_gen.py
(and the Rust edge's backend/src/contract_gen.rs) by contract/gen.py, and re-exported
here so existing `from provision.ids import ...` callers keep working unchanged. This
is the single source of truth for the cross-language identity/key/token grammar (see
CHARTER): a mismatch would silently route a real device into the `default` garden, or
make the edge look up a token under a different secret name than this provisioner
wrote. Edit contract/spec.toml, run `make gen`; `make gen-check` (in make ci) fails
the build on drift.
"""
from .contract_gen import (  # noqa: F401  (re-exported for back-compat)
    DEFAULT_GARDEN,
    TOKEN_SLOT_CURRENT,
    TOKEN_SLOT_PREVIOUS,
    is_valid_id,
    token_secret_name,
)
