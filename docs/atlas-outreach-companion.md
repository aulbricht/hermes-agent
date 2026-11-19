# Atlas neighbor outreach draft companion (`SNO-3974`)

Same-vendor review: Gemini was down at Nov 16, 9:49 PM PST (historical mark covering the signed Codex attack on [SNO-3851](/SNO/issues/SNO-3851), not a current outage; normal-vendor re-check obligation preserved).

## Scope and lineage

This change implements the bounded Hermes companion prerequisite ([SNO-3974](/SNO/issues/SNO-3974)) for [SNO-3848](/SNO/issues/SNO-3848) under the signed contract of [SNO-3851](/SNO/issues/SNO-3851) revision 5 (`1cbf3f99-8c5b-4bac-a73d-0eb99dccfafa`) and [SNO-3974#document-plan](/SNO/issues/SNO-3974#document-plan) revision 3 (`9e9f6aaa-ff66-4913-a523-f189211b0d2f`).

- **Qualifying base:** `d4e6dd8de87ae2470ca4ef23e145b67dce4ddcc9` (`origin/main`, merging [PR #32](https://github.com/aulbricht/hermes-agent/pull/32) for [SNO-3977](/SNO/issues/SNO-3977)), verified to contain both required email baseline `ed3774867c07299b2178f18b936b1c06b46be0c4` and accepted main `976e25fc671fb2a7fa2154f92bb0aef963eadc33` (`git merge-base --is-ancestor` exits `0` for both).
- **First tests-only commit:** `681e5a9ee` (`test(atlas): commit signed SNO-3851 revision 5 outreach gateway attack fence`), committing `tests/gateway/test_atlas_outreach_attack.py` unchanged (404 lines, SHA-256 `328b1b937912d63b390f64a49637a386b0faff84a0e061061e2052bdee2ae21a`).
- **Overlap coordination with [SNO-3929](/SNO/issues/SNO-3929):** inspected `origin/main...paperclip/SNO-3929` ([PR #33](https://github.com/aulbricht/hermes-agent/pull/33)), which modifies only `.gitignore`, `docs/atlas-parcels-contract.md`, `tools/atlas_ownership_tool.py`, and three `tests/gateway/test_atlas_ownership_parcels_*.py` files. Zero files overlap with `SNO-3974`.
- **No alternate read of refused helper:** `agent/agent_runtime_helpers.py` was not read or modified; explicit turn-context refusal gates are enforced in `model_tools.handle_function_call`, `tools.registry.ToolRegistry.dispatch`, and `agent.tool_executor`.

## Bounded implementation

1. **Save-only outreach tool (`tools/atlas_outreach_tool.py`, `toolsets.py`, `tools/delegate_tool.py`):**
   - Registers `atlas_outreach_save` in toolset `atlas_outreach` (and blocks delegation in `DELEGATE_BLOCKED_TOOLS`).
   - Accepts only the six adopted payload keys (`subject_property_id`, `owner_id`, `email_subject`, `email_body`, `call_script`, `facts` with `[{source_ref, path, value}]`), requiring non-empty strings for all text fields and rejecting unknown/missing keys, non-JSON values, or embedded capability tokens (`atqry_`, `atown_`, `atimp_`, `atmail_`, `atout_`).
   - Enforces the 65,536-byte UTF-8 compact sorted JSON payload bound and 131,072-byte raw callback request bound before calling fixed loopback `POST http://127.0.0.1:8243/api/v1/internal/outreach-draft-tool` with `{"protocol": "atlas-outreach-drafts-v1", "operation": "save", "arguments": <payload>}` and `Authorization: Bearer <token>`.
   - Uses `httpx.Client(timeout=httpx.Timeout(30, connect=2), follow_redirects=False, trust_env=False)`, enforces a 65,536-byte response bound, validates the exact `{"protocol": "atlas-outreach-drafts-v1", "ok": True, "result": {"id": ..., "run_id": ...}}` receipt without token echo, and returns `{"error": "Outreach operation unavailable"}` for any missing/invalid authority or callback refusal.
2. **Authority-aware discovery and independent dispatcher/registry isolation (`model_tools.py`, `tools/registry.py`, `agent/tool_executor.py`, `tools/env_probe.py`):**
   - `get_tool_definitions` includes `outreach_bound` and `outreach_allowed` in its memoization cache key. When `outreach_capability_available()` is bound and `atlas_outreach` is enabled, the effective tool catalog is clamped to `{"atlas_outreach_save"}` and Tool Search progressive-disclosure assembly is skipped. When unbound, `atlas_outreach_save` is excluded.
   - While `outreach_capability_available()` is bound, `model_tools.handle_function_call`, `tools.registry.ToolRegistry.dispatch`, and `agent.tool_executor` (`execute_tool_calls_sequential` / `execute_tool_calls_concurrent`) independently refuse every function name other than `"atlas_outreach_save"` (including `tool_call`, `tool_search`, `tool_describe`, `web_search`, `terminal`, `atlas_email`, `atlas_ownership_proposal`, `atlas_query`, and agent-loop tools) with `{"error": "Outreach operation unavailable"}` before argument coercion, Tool Search bridge unwrapping, request middleware, `pre_tool_call` hooks, or registry entry lookup.
   - `tools.env_probe` (`warm_environment_probe_async` and `get_environment_probe_line`) skips local Python toolchain subprocess probing while `outreach_capability_available()` is bound so outreach turns spawn zero subprocesses.
3. **Gateway admission and executor-local binding (`gateway/platforms/api_server.py`):**
   - `_parse_session_turn_options` validates any present `atlas_outreach_capability` field against `atout_[A-Za-z0-9_-]{32,128}` and rejects `null`, empty, malformed, or mixed (`atlas_import_capability`, `atlas_ownership_capability`, `atlas_query_capability`, `atlas_email_capability`) authority with HTTP 400 before `_run_agent` is called, without echoing or logging tokens.
   - `_run_agent` binds `bind_outreach_capability(atlas_outreach_capability or "")` inside its worker thread executor before `_create_agent` and resets via `clear_outreach_capability(outreach_token)` in `finally` across normal completion, exceptions, and cancelled awaits.
   - `_create_agent` strips `atlas_outreach` from default platform toolsets and, when `atlas_outreach_authorized=True`, replaces `enabled_toolsets` with `["atlas_outreach"]` regardless of absent, empty, or broad `toolset_scope`.

Chose `ContextVar`-gated catalog filtering in `model_tools.py` over a `registry.register(..., check_fn=...)` callback because `tools/registry.py` caches `check_fn` results globally across threads for 30–60 seconds (`_CHECK_FN_TTL_SECONDS` / `_CHECK_FN_FAILURE_GRACE_SECONDS`), whereas outreach authority is per-turn and executor-thread-local; flag if you'd prefer a non-cached registry flag.

## Frozen attack test shapes and coverage (`tests/gateway/test_atlas_outreach_attack.py`)

Committed unchanged from [SNO-3851](/SNO/issues/SNO-3851) revision 5 (`1cbf3f99-8c5b-4bac-a73d-0eb99dccfafa`):

- **Authority-aware schema cache transitions and fixed loopback callback envelope (`PAYLOAD`, `TOKEN`, repeated bind/clear cycles):** `test_definition_cache_transitions_and_fixed_callback`.
- **Direct and Tool Search bridge calls refused before pre-tool hooks, middleware, or registry dispatch while outreach is bound (`FORBIDDEN`: `web_search`, `terminal`, `atlas_email`, `atlas_ownership_proposal`, `atlas_query`, `tool_call`):** `test_bound_dispatch_denies_before_middleware_or_registry`.
- **Direct `registry.dispatch` calls refused before `registry.get_entry` lookup while outreach is bound (`FORBIDDEN`):** `test_direct_registry_also_denies_before_entry_lookup`.
- **Ordinary unbound dispatch control (`web_search` when outreach is unbound):** `test_unbound_ordinary_dispatch_keeps_existing_path`.
- **Real gateway `/api/sessions/{session_id}/chat/stream`, `_create_agent`, `AIAgent` loop, toolset scope (`None`, `[]`, `["web", "terminal", "atlas_email"]`), allowed save and forbidden direct/bridge calls, and zero token leakage:** `test_real_gateway_agent_and_dispatch`.
- **Mixed authority rejection at gateway admission (`atlas_import_capability`, `atlas_ownership_capability`, `atlas_query_capability`, `atlas_email_capability` combined with `atlas_outreach_capability`) with zero token echo:** `test_gateway_rejects_mixed_authority_before_execution`.
- **Present malformed/empty/null outreach authority rejection at gateway admission (`""`, `"forged"`, `1`, `[]`, `None`):** `test_gateway_rejects_malformed_present_authority`.
- **Single-worker executor ContextVar binding before `_create_agent`, reset in `finally`, and cleanup after `RuntimeError`:** `test_real_executor_binding_reset_and_exception_cleanup`.
- **Concurrent multi-worker executor isolation (`TOKEN` vs `OTHER` across two threads synchronized on a barrier):** `test_concurrent_executor_users_keep_their_own_token`.
- **Cancelled `await` on `_run_agent` with late callback refusal (`403` -> `{"error": "Outreach operation unavailable"}`) and subsequent worker cleanup:** `test_cancelled_await_leaves_late_callback_denied_and_then_cleans`.

## Verification receipts

1. **Unchanged attack fence hash and line count:**
   - `sha256sum tests/gateway/test_atlas_outreach_attack.py && wc -l tests/gateway/test_atlas_outreach_attack.py`
   - Output: `328b1b937912d63b390f64a49637a386b0faff84a0e061061e2052bdee2ae21a  tests/gateway/test_atlas_outreach_attack.py` and `404 tests/gateway/test_atlas_outreach_attack.py`.
2. **Baseline failure on tests-only commit `681e5a9ee`:**
   - `python3 -c "from tools import atlas_outreach_tool"` -> `ImportError: cannot import name 'atlas_outreach_tool' from 'tools'` (exit 1).
   - `scripts/run_tests.sh tests/gateway/test_atlas_outreach_attack.py -q` -> `error: no virtualenv found in .../.venv or .../venv` (exit 1; **not verified locally**, runs on GitHub Actions CI).
3. **Post-implementation local syntax, whitespace, attribution, and stdlib behavioral checks:**
   - `python3 -m py_compile tools/atlas_outreach_tool.py toolsets.py model_tools.py tools/registry.py agent/tool_executor.py tools/delegate_tool.py gateway/platforms/api_server.py tests/gateway/test_atlas_outreach_attack.py` -> exit 0.
   - `git diff --check` -> exit 0.
   - Stdlib behavioral harness exercising `test_definition_cache_transitions_and_fixed_callback`, `test_bound_dispatch_denies_before_middleware_or_registry`, `test_direct_registry_also_denies_before_entry_lookup`, `_parse_session_turn_options` malformed/mixed/scoped cases, `test_real_executor_binding_reset_and_exception_cleanup`, `test_concurrent_executor_users_keep_their_own_token`, and `test_cancelled_await_leaves_late_callback_denied_and_then_cleans` -> exit 0 (`All stdlib checks passed!` / `All async executor and parser checks passed!`).
   - `.github/workflows/contributor-check.yml` attribution check against `origin/main` (`d4e6dd8de87ae2470ca4ef23e145b67dce4ddcc9`) -> `✅ All contributor emails are mapped in AUTHOR_MAP.` (exit 0).
   - `scripts/run_tests.sh tests/gateway/test_atlas_outreach_attack.py tests/gateway/test_atlas_ownership_tool.py tests/gateway/test_atlas_query_tool.py tests/gateway/test_atlas_email_tool.py -q` -> **not verified locally** (no local virtualenv in the Paperclip worktree; executed by the GitHub Actions CI suite).

## Review, CI, risk, and rollback

- **Gates:** Tier 3 companion change requires green exact-head GitHub Actions CI and independent Code Review (Codex), Security Reviewer, and Verifier acceptance on this draft PR before [SNO-3848](/SNO/issues/SNO-3848) pins the companion SHA in Atlas. Every Atlas-project merge remains gated on Allen's per-merge docket approval and the Atlas Chief PM's authorized merge route.
- **Risk and rollback:** All new behavior is gated on per-turn `atlas_outreach_capability` (`atout_...`). Ordinary chat, ownership, query, import, and email turns take their existing code paths. Reverting the implementation commit (or the PR merge) cleanly removes `atlas_outreach_save` and restores the baseline state.
