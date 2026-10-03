# Fixed Atlas main/Support resolution binding

Updated October 2, 2026. Source candidate; not deployed or accepted as a
complete Phase 2 production baseline. This extends the historical v1 runtime
evidence publisher. It does not replace its receipt or convert Python code
fingerprints into source-byte attestation.

## Owner capture and actual consumption

`APIServerAdapter._resolve_agent_constructor_inputs` preserves the existing API
resolution order for ordinary and guarded construction: runtime provider/auth
outcome, reasoning, primary model, runtime fallback model override, cached alias
unless an in-memory session override exists, platform tools, iteration cap, and
configured fallback. Ordinary callers retain their original initializer reads
and no Atlas snapshot is seeded on ordinary agents.

The private owner initialization seam is:

```python
generation_id = adapter._prepare_atlas_resolution(
    gateway_session_key=fixed_worker_session_key,
    model_alias="default",  # or the existing atlas-luna / atlas-sol alias
)
```

At API adapter startup, `connect()` now invokes the owner preparation seam for
the dedicated worker `atlas-phase2-ownership-v1`, role `main`, alias `default`,
before starting the HTTP listener. Existing gateway startup completes MCP
discovery before connecting adapters. Preparation runs in a thread so its
existing credential/config/tool reads do not block the event loop; it creates
no agent and dispatches no request. Support, other ports, disabled Atlas routing,
and public bindings do not receive this preparation.

Each startup preparation discards any earlier publication for this worker while
retaining the store's monotonic counter. A failed preparation leaves its probe
unavailable and its reserved worker unable to use legacy unguarded construction.
The ordinary API service can still start. Private resolver errors are not echoed.
Probes and inbound requests cannot perform startup preparation or retry it.

The dedicated Phase 2 worker is **tool-free**. Its existing model/provider/effort,
context/output limits and accounting policy are retained, while actual constructor
toolsets, schemas and available tool names are empty. The captured semantic
projection records `tool_access: "none"`; that policy belongs to the fixed worker
binding, not to a request override or a system prompt. Its main profile can still
configure MCP servers for ordinary callers; those configurations do not become
this worker's tool availability.

Construction and every main dispatch verify the empty tool surface. Outbound
tools/functions or tool-choice controls cannot reintroduce it, including through
provider extra-body fields. The real conversation loop rejects normalized
model-returned tool calls before post-response hooks, invalid-name retry handling,
batch classification, callbacks, middleware or registry/builtin execution. A
sticky denial propagates through the loop's error handler without another model
request. Sequential,
concurrent and individual invocation entry points enforce the same captured deny.
A denied attempt prevents further dispatch and a successful acceptance receipt,
even if conversation error handling catches the rejection. Ordinary main/Support
callers retain their tool behavior.

It uses the actual shared resolution path and captures initializer config, actual
tool schemas/registry generation, inspected constructor defaults, nonsecret
admission policy, and private selected credentials. It does not construct an
agent or perform inference. Its existing provider credential resolution and tool
selection effects belong to owner initialization; they are never invoked by a
metadata probe. Selected lazy implementation imports are loaded before capture.

Owner capture publishes one `PreparedTurn`. Guarded construction resolves current
inputs through the same path and checks semantic settings, dependencies, full
private config, and private selected credentials against the expected generation
before calling `AIAgent`. The accepted constructor consumes the **original
published snapshot**, not a freshly recaptured equivalent object. Request text,
history, callbacks, transcript ID, and session DB are outside that resolution and
are supplied per call.

The real initializer consumes the supplied config for its five direct config
reads, timeout/default-header helpers, and context/output configuration; it uses
the captured tool schemas/generation. Main Responses client timeout/header
rebuilds consume the same snapshot. Native and auxiliary paths remain outside
this contract.

## Non-inference probe

`GET /v1/atlas/resolution-generation` requires the existing API Bearer key,
loopback binding/peer, existing Atlas routing enablement, fixed role inferred from
the existing main port 8093 or Support port 8092, and a fresh
`X-Atlas-Baseline-Nonce` matching `[A-Za-z0-9_-]{16,128}`. Queries are rejected.
Responses use `Cache-Control: no-store` and protocol
`atlas.hermes-resolution-generation.v2`.

The probe reads owner-held records and loaded/cached dependencies. It never
loads config, resolves credentials, discovers tools, constructs an agent, or
performs inference. It returns `unavailable` until owner preparation succeeds;
entries are `ready_conditioned_on_admission` or `invalidated`. Fresh observation
time does not replace original resolution time. Generation is process-bound and
monotonic; Linux `/proc` start identity is required for publication.

Session keys, API keys, full config, prompts, and tool-schema contents are not
exported. Binding hashes, selected safe settings, schema fingerprints and selected
loaded Python fingerprints are distinct evidence. Known cached alias, session,
policy, registry, selected environment, constructor-default, or loaded-code drift
invalidates readiness. Unknown disk/private credential changes are checked during
fresh actual admission; probe readiness alone never authorizes inference.

## Fixed guarded request

`POST /v1/atlas/guarded/chat/completions` requires the same fixed access conditions
plus:

- `X-Hermes-Session-Key`: the owner-prepared fixed worker binding.
- `X-Atlas-Resolution-Generation`: the expected 64-character lowercase hex ID.
- Body: `messages`, optional `model`, and optional literal `stream: false` only.
  Models are the existing default or prepared `atlas-luna` / `atlas-sol` alias.
- No query parameters, streaming, or `Idempotency-Key`. Additional request
  controls are rejected for this first fixed scope.

The generation binding is explicitly copied into the real thread executor.
Checks run before construction, after initialization, before conversation start,
and immediately before each wrapped main Responses dispatch. An explicitly
registered fixed worker cannot use an unguarded legacy construction surface.
Other ordinary callers keep their behavior. A fixed client must preserve its
registered role/session/alias binding and must not retry a rejection through a
different worker identity or ordinary endpoint.

Dispatch checks bind model/provider/API mode/effort, output and configured context
limits, actual tool schemas and permitted Responses schema subset, registry
generation, selected constructor defaults, and private credentials. Default
request overrides are empty; unsupported sampling is rejected. Sol dispatch
still requires the existing atomic accounting reservation. Denied/unavailable
admission may select Luna `xhigh` only with a gate-issued per-turn fallback marker;
an arbitrary model mutation cannot authorize that route. The frozen prepared
policy drives routing/reservation/settlement after live policy comparison.

A successful response requires at least one validated dispatch and includes
`X-Atlas-Accepted-Resolution-Generation` and an `atlas_resolution` receipt with
`generation_id` and `dispatch_count`. No dispatch cannot masquerade as an accepted
response. Drift returns a generic 409; unavailable/internal guarded failure is a
generic 503. Private resolver/client exceptions are not echoed.

## Explicit limitations and integration dependencies

- Main/default startup preparation for `atlas-phase2-ownership-v1` is implemented
  locally. It is not deployed. Other worker/alias preparation remains an explicit
  owner step; no automatic Support publication is added. Probe collection cannot
  provide readiness.
- `default` resolves to Luna but is a distinct generation scope from the
  `atlas-luna` alias. A client requiring `model_alias: "atlas-luna"` cannot consume
  the default-only startup generation; that alias requires separate owner
  preparation or a reviewed change to the startup contract. No inbound request
  can prepare an alias or switch the binding.
- The Phase 2 owner must wire its fixed caller to this endpoint and require the
  accepted-generation receipt; its separate checkout is untouched here.
- Session overrides and nonempty configured/active provider fallback chains are
  unsupported and fail closed in guarded scope; ordinary behavior is preserved.
- Credential pool identity and selected key are checked privately. Pool internal
  mutation, provider acceptance, and external account state are not attested.
- When context is omitted in Support config, the omission is preserved. Effective
  compressor context is recorded and checked at construction/dispatch as a
  construction-only observation, not a promised provider-default numeric limit.
- Budget reservation eligibility remains per-call. Accounting policy generation,
  allowances/day/month owner proof, external BYOK account metadata, complete
  source-byte/startup attestation, classifier/planner/report/title/compression,
  native gateway, and auxiliary execution are explicitly not covered.
- No new model/provider/effort/budget/tool settings, CI, activation, configuration,
  credentials, deployment, or production cleanup are part of this implementation.

## Verification

Behavioral fixtures exercise real temp-profile config resolution, registered
HTTP endpoints, executor propagation, pre-factory drift, worker binding, denied
legacy fallback, omitted Support limits/zero tools, and pre-dispatch drift. Real
`AIAgent`/initializer tests use inert clients and test captured config/tool/timeout/
header consumption. Main Responses payload/admission fixtures use the actual
builder with fake dispatch/accounting boundaries. Run through
`scripts/run_tests.sh`; see the current implementation receipt for exact results.

Dedicated-worker fixtures use injected untrusted source text, fabricated provider
tool calls, real construction/request building, actual tool execution entry points,
and attempted schema/availability restoration. Ordinary main construction and
actual synthetic tool invocation remain covered separately.

Independent review and combined current-tree tests are required before this local
candidate is considered ready for a separately authorized delivery.
