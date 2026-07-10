# gp-provision — Fastly Garden Protector control plane

The **single writer** of the registry (`index/gardens`, `index/g/<gid>/devices`),
the **minter of per-garden auth tokens**, and the **one-token deployment
provisioner** (Compute service + KV/Secret stores + Fastly Object Storage bucket +
CDN read-signing service).

## Why a control plane (generic + Fastly framing)
Multi-tenant onboarding needs a single authority that assigns ids and credentials —
the edge devices must not self-register. **Fastly KV Store** (the edge key-value
database) has no compare-and-swap, so a concurrent read-modify-write of the registry
would lose updates. This process is the **only** writer; it computes the whole
registry blob and PUTs it. The Pi never touches the registry — it just carries the
ids + token this tool assigns (RFC §4). Per-garden tokens live in a **Fastly Secret
Store** (edge secrets management) under the slash-free name `g.<gid>.token_current`.

## Install / invoke
Deps (already typical for this repo): `typer`, `rich`, `boto3`, `requests`, `tenacity`.
Run as a module — no install needed:

```bash
python -m provision.cli --help
```

Token resolves from `--token` → `$FASTLY_API_KEY` → interactive prompt.

## Commands
| Command | What it does |
|---|---|
| `provision --service-name NAME` | Create/reuse the Compute service, KV (`garden_state`, `garden_models`) + Secret (`garden_tokens`) stores (resource-linked), upload the model, deploy the prebuilt Wasm, (unless `--skip-archive`) stand up the FOS bucket + CDN read-signing service, seed `index/gardens`, write `configs/{service_id}.json`. Auto-rolls-back on any failure. |
| `seed-registry` | Ensure `index/gardens` has the `default` garden. |
| `register-device --garden G --device D --kind observer|deterrent --type T` | Validate id charset + uniqueness + taxonomy, ensure the garden exists, **mint the garden's token** (non-`default`), write `index/g/G/devices`, and emit the Pi's `configs/G-D.env` (gitignored). |
| `rotate-token --garden G` | current → previous, mint a new current (both valid during the window). The prior token is read from `GP_PRIOR_GARDEN_TOKEN` (preferred — keeps the secret off argv / the process list) or, for a direct interactive run, `--prior-token T`. |
| `teardown` | Destroy the service + stores (+ bucket with `--remove-data`). **Requires a `global`-scoped `--token`; never falls back to a stored key.** |

`--type` accepts cameras (`camera_csi/usb/rtsp`), non-camera observers
(`motion_pir`, `heat_thermal`, `ir_break_beam`, `sound_mic`, `contact_switch`),
deterrents (`water_pump`, `solenoid`, `strobe`, `shelly_switch`, …), **and any
charset-valid custom type** — so other devices that plug into a Pi work without a
code change.

## Deploy flow (one Fastly token)
```bash
# 1. Build the edge package (committed-package deploy needs no toolchain on the host)
cd backend && fastly compute build && cd ..

# 2. Provision (core only first; add FOS/CDN by dropping --skip-archive)
FASTLY_API_KEY=*** python -m provision.cli provision --service-name garden-protector --skip-archive

# 3. Register a multi-device garden (camera + motion sensor) — mints the garden token
python -m provision.cli register-device --service-id <sid> --garden backyard \
    --device cam-front --kind observer --type camera_usb
python -m provision.cli register-device --service-id <sid> --garden backyard \
    --device pir-1 --kind observer --type motion_pir

# 4. On the Pi: source the gitignored deploy env, then run the client
set -a; . configs/backyard-cam-front.env; set +a
python3 hardware/client.py --monitor --backend "$GP_BACKEND"
```

## Verification status
The auth + admin/registry + per-garden control + edge ONNX-inference loop is
**verified end-to-end on a live Fastly account** (provision → register a
camera+motion+heat garden → token enforced, cross-garden replay rejected, control
drives the heartbeat). The evidence-archive **PUT→CDN-read round-trip is verified end-to-end on a live
Fastly account** (2026-06-22): a non-`--skip-archive` provision wires the edge's
`cdn_read` backend + `cdn_host`/`fos.cdn_secret`, an evidence POST archives the JPEG
to FOS, and `GET /api/archive/image` serves it **through the CDN** — confirmed by the
edge log (`component=archive op=cdn_get outcome=ok status=200`), a direct
`/img/<key>` read returning identical bytes (`x-cache: HIT`), and the read gate
rejecting a missing secret (401). Reads carry a 30-day `immutable` Cache-Control
(images are content-addressed by unique keys), added by both the edge response and
the CDN VCL since FOS sets none. (The best-effort archive WRITE was observed to be
intermittent under rapid repeated POSTs — only some land in the bucket; that's the
unchanged `handle_evidence` best-effort PUT, off the fail-closed path, and is a
separate reliability follow-up.) NOTE: Fastly Secret Store updates take ~15-20 s to
propagate to the edge, so a freshly-minted/rotated token is not usable immediately.

## Smoke test (offline, no Fastly account)
```bash
# Unit tests (pure logic + mocked API):
python3 -m pytest tests/test_provision.py -q

# Dry-run the CLI against canned responses (records calls, writes a deploy env):
FASTLY_MOCK_MODE=1 python -m provision.cli register-device --service-id MOCK \
    --garden backyard --device cam-front --kind observer --type camera_usb
```

## Secrets / teardown
`configs/{service_id}.json` holds live FOS keys, `cdn_secret`, the Fastly admin
token, and store ids; `configs/*.env` holds per-device Pi tokens. **Both are
gitignored** (`configs/*` keeps only `.gitkeep`). Teardown removes the service +
stores; pass `--remove-data` to also empty + delete the FOS bucket. Teardown
demands a `global`-scoped token and will not reuse the stored admin key.
