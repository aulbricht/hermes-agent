# Atlas Hermes runtime evidence candidate

Status: local candidate; not deployed. Base Hermes revision:
`570e60b318fc84d8db208aa2ad3ae4d6df500ed5`.
No model, provider, reasoning, routing, budget, credential or existing API behavior
is changed. This adds an owner metadata endpoint and a bounded observation at the
existing Atlas admission dispatch boundary. It never creates an agent, requests
inference, reloads config, queries a provider or writes receipts on a metadata read.

## Endpoint and authentication

`GET /v1/atlas/runtime-evidence`

- Enabled only when the adapter was constructed with the existing Atlas routing
  flag enabled and its bind address is exactly `127.0.0.1` or `::1`.
- The HTTP peer must also be loopback. The existing API server Bearer key is
  required, including in manually constructed adapters. No key means denial.
- Required header: `X-Atlas-Baseline-Nonce`, 16–128 ASCII letters, digits,
  underscores or hyphens. No query parameters. No paths, commands, PIDs or models
  can be selected by the caller.
- Disabled/nonlocal: 404; unauthorized: 401; malformed nonce/query: 400;
  unavailable evidence: generic 503. Success sends `Cache-Control: no-store`.
- Responses use protocol `atlas.hermes-runtime-evidence.v1`. The owner collector
  must generate a new unpredictable nonce for each request, require exact echo,
  match PID/start ticks/boot ID to its supervisor/proc observation, and enforce
  its existing age limit (30 seconds, at most 60). The nonce is a binding against
  accidental replay, not a replacement for authenticated transport or trust in
  the local process. No request values are persisted by the publisher.

## Evidence and boundaries

`process` contains the live PID and, on Linux, `/proc/self/stat` start ticks and
kernel boot ID. Unsupported platforms explicitly report unavailable start
identity; the collector must not invent it or accept PID alone.

`loaded_code` fingerprints currently installed Python function/method code
objects in a fixed module list. A missing module is `not_loaded`, not imported
for the sake of the probe. Fingerprints use canonical immutable code data
(bytecode, names, flags, locations and recursively typed constants), SHA-256 per
function, then canonical JSON; they are Python-version/path dependent. The
publisher fingerprints its own collection code too. `python_cache_tag`
must match any reviewed reference. Replacing loaded functions changes evidence;
editing a source file on disk without reloading does not.

These fingerprints do **not** attest the complete source bytes: module-level
execution, mutable globals, function defaults/closures, native dependencies and
client/SDK internals are not included. `loaded_source_sha256` is deliberately
null. Do not substitute these hashes for the collector's source-byte hashes.

`adapter_routes` projects actual cached alias routes with strictly allowlisted
model/provider/effort values, numeric iteration limits and credential-override
presence. It is not a fresh disk read or a whole loaded profile. Unsupported
values are null, never echoed. Its canonical hash excludes credentials.

`last_dispatch` is null until the existing Atlas admission wrapper observes an
actual dispatch attempt. It contains the original observation time, a safe
projection of the agent's data attributes, the actual outbound payload's model,
effort, token cap, provider selection and service tier, hashes of both projections,
and the admission code fingerprint at that attempt. It records denied Sol's
Luna fallback after rewriting. It contains no prompt, key, user/session ID,
provider response or client object. Missing/unrepresented attributes are null.
The profile hash covers only the exported projection, not every effective
setting (for example MCP and auxiliary configuration are not captured).

A fresh metadata response does **not** refresh the last-dispatch timestamp. The
snapshot can be old, from a completed/failed attempt, or precede a later settings
change. It is not the next turn's resolved profile, proof of HTTP transmission,
provider acceptance, or universal coverage of auxiliary/provider paths. Observation
is bounded to one retained profile per process; multiplexed use is unsupported.
`loaded_profile_sha256` is deliberately null because Hermes resolves several
settings again each turn. Observation failures do not change dispatch behavior.

`external_byok_state` is always `not_observed`. A local request's OpenAI-only
policy does not establish the mutable OpenRouter account's BYOK/shared-capacity
settings. No provider query is part of this endpoint.

All JSON hashes use sorted keys, compact separators, ASCII escaping, no NaN.
Semantic hashes exclude observation time and nonce. The endpoint is owner
metadata only; it is not a model tool or a new public readiness assertion.

## Phase 2 integration and remaining owners

The current `OwnerRuntimeCollector` in `scripts/acre_phase2_runtime_baseline.py`
requires seven reviewed source-byte hashes, complete main/support profile hashes,
fresh provider-workspace policy, serving release provenance and activation/health
evidence. This Hermes candidate supplies **partial evidence only**. It cannot be
passed directly as `owner_metadata()` or used to make that collector pass.

The integration owner must keep its exact freshness/hash checks and fail closed:

1. Fetch main/support endpoints with their existing protected API credentials,
   fresh nonces and bounded timeouts/response sizes; validate this protocol.
2. Validate process identity and Python/code references separately from file
   hashes. Never rename `loaded_code.sha256` to `loaded_source_sha256`.
3. Retain original last-dispatch age and hash binding. Missing, partial, stale,
   or mismatched profile/policy evidence blocks acceptance.
4. Provide a separately reviewed startup source-byte attestation/import binding
   and complete resolved-profile contract if Phase 2 requires those proofs.
   Backend/classifier/broker/accounting/Support owners also need their own loaded
   evidence; their files are not attested by a Hermes process.
5. Obtain external account policy from a separately authorized protected management
   metadata observer. Historical activation receipts are not fresh BYOK proof.
6. Delivery owner reviews and deploys a pinned candidate through its normal gate.
   This work neither repairs enrollment nor authorizes activation or pilot inference.

Focused verification: `scripts/run_tests.sh
tests/gateway/test_atlas_runtime_evidence.py tests/agent/test_atlas_sol_budget.py
-j 2 -q`. Tests use temporary Hermes homes and stub dispatch only; endpoint tests
start the real adapter on ephemeral loopback ports. No external provider calls.
