# CHARTER — Garden Protector

**This file governs this repository.** Where it conflicts with [AGENTS.md](AGENTS.md),
the Charter wins. AGENTS.md remains the canonical guide for *how the system works* and the
[Traps & Gotchas](AGENTS.md#traps--gotchas); the Charter records the *standing engineering
decisions* that AGENTS.md's demo-farm defaults would otherwise contradict.

## Status: a product, not a demo

Garden Protector started life on the Fastly "demo farm" — a fleet of small, deliberately
*diverse* example repos whose job was to teach one Fastly concept each. It has since
graduated. It is now a single, real, hardware-validated product with one Pi gateway, one
ESP32-C3 node, one Fastly Compute edge, one Python control plane, ~360 passing tests, and a
live deployment. It is maintained as **one coherent codebase**, not as a showcase.

That change of status flips two demo-farm mandates from "good" to "harmful":

### Exemptions from AGENTS.md

* **EXEMPT from "Organic Diversity (CRITICAL)" ([AGENTS.md](AGENTS.md) §"Operating Posture",
  the *Organic Diversity* bullet).** The demo farm wanted repos to look hand-rolled and
  varied so they didn't read as a uniform corporate pitch. Inside *one* product, that
  mandate manufactured the exact accreted duplication this repo is now converging away from
  — eight hand-written `<style>` blocks, two copies of the scrypt KDF, four bespoke HTTP
  routers. Within this repo we standardize deliberately: one palette, one auth library, one
  router pattern, one contract source of truth.
* **EXEMPT from "The Dual-Terminology Rule (CRITICAL)" ([AGENTS.md](AGENTS.md)
  §"The Dual-Terminology Rule").** Pairing every Fastly term with a generic synonym
  ("Fastly Compute / serverless edge computing / WebAssembly application") in code comments
  and internal docs is SEO scaffolding for a searchable example. It adds noise to a product
  codebase. User-facing README/marketing copy may still use both vocabularies where it helps
  a reader; **internal code, comments, and developer docs need not.**

No other part of AGENTS.md is waived. In particular **the Traps & Gotchas list, the
fail-closed/safety-path rules, the telemetry-is-observe-only rule, and "keep AGENTS.md
current" all still bind.**

## Standing decisions (the convergence targets)

These are *decided*. Re-opening them needs a Charter edit, not a code review:

1. **One shared UI asset layer.** A single CSS palette/primitives file and a single JS
   helper module (theme toggle, `fetch` wrapper, SSE helper, nav-as-data) are the source of
   truth for every HTML page (the edge dashboard + all portal/console pages). A nav or
   palette change is a one-file edit. Pages assemble head + nav + body from one seam per
   tier. *(Built in Phase 3.)*

2. **One shared Python service library.** The scrypt KDF, auth-record make/verify,
   `RateLimiter`, `SessionStore`, LAN-gate helper, the `EdgeClient` (per-garden header
   injection over `requests`), and the table/decorator router are each defined **once** and
   imported by both the Pi portal (`hardware/portal.py`) and the console
   (`provision/console.py`). No second copy of crypto, rate-limiting, or edge-proxy logic.
   *(Built in Phases 1–2.)*

3. **Pinned toolchains.** The Rust toolchain (channel + `wasm32-wasip1` target) is pinned in
   `backend/rust-toolchain.toml`; Rust dependency versions are pinned by `Cargo.lock`. Python
   dependency versions are pinned in `constraints.txt` (the `>=` floors in the per-tier
   `requirements.txt` files stay as floors so a platform without a matching wheel can still
   install the core; the constraints file freezes the known-good versions). Builds are
   reproducible; "works on my machine" toolchain drift is designed out.

4. **One identity/contract source of truth.** The cross-language drift set — id charset +
   `DEFAULT_GARDEN`, KV key / dev-key templates, the `token_secret_name` template, the
   archive-key grammar (incl. the `86400 − seconds_of_day` inversion), the identity/trace
   header set, the three safety-decision functions (`apply_control`, rain-suppress,
   node-liveness threshold), and the cost-usage struct — lives in **one flat spec**
   (`contract/spec.toml`) and is **generated** into both the Rust edge and the Python tiers.
   `make ci` fails if the checked-in generated files drift from the spec. Hand-copying a
   shared constant into two languages is forbidden. *(Built in Phase 4.)*

## Non-goals (proportionality)

This is a solo, real-but-small product. The convergence above is *deletion of duplication*,
not a rewrite. Explicitly **not** adopted:

* No React/Vue/Svelte or any web framework. The pages stay vanilla HTML + JS.
* No `http.server` replacement. The Pi portal and the console stay on the Python stdlib
  `http.server`; they are **not** merged into one service and **not** ported to a framework.
* No OpenAPI/protobuf/Jinja2 templating engine. The page-assembly seam is a plain f-string
  (Python) / `format!`+`include_str!` (Rust).
* The edge stays Rust/WASM. It is **not** ported to Python.
* Per-garden KV key namespacing, the inverted archive key, and the scattered fail-closed
  branches are **correctness features**, not duplication. They are preserved, not "simplified
  away."

When a change could go big or small, take the smaller one and leave a note.

## The safety contract (never traded for tidiness)

No refactor in this repo may add latency to, or change the behavior of, any of: `/api/evidence`,
`/api/status`, the `/api/telemetry` heartbeat, the gateway spray core, or any fail-closed
branch. Telemetry is observe-only and off the safety path. A refactor that must touch a safety
path lands a test pinning current behavior **first**. The fail-closed invariants are codified
in `tests/test_safety_properties.py` and the `backend/src/main.rs` test module, and are part of
`make ci`.
