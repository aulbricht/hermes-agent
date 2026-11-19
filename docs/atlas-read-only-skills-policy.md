# Read-only skills and workflow scopes: implementation checkpoint

Source work for SNO-4027. Not accepted, published, pinned or deployed. Publication is
held on the frozen Atlas integration fixture conflict described below. The code
roles are capability/privacy guardrails and platform plumbing; no workflow judgment
or new model tool is introduced.

## Configuration and callable contract

The profile's `config.yaml` supports:

```yaml
agent:
  tool_policy:
    version: 1
    denied_tools: [skill_manage, memory]
    require_explicit_scope: true
skills:
  external_only: true
  external_dirs: [/absolute/immutable/released-skills]
  inline_shell: false
memory:
  memory_enabled: false
  user_profile_enabled: false
  provider: ""
```

Atlas packaging must also supply its existing `.no-bundled-skills` marker. The
absolute release path above is an example, not an installed path. The release
owner must make the root immutable and usable before activation. Initialization
refuses an unusable root; discovery becomes empty if it disappears later.
External-only index/list/view omit local, bundled and outside plugin content,
stale local snapshots, and resolved skill/support paths outside approved roots.
Bundled sync is skipped. Defaults retain ordinary Hermes discovery and management.

`tools.profile_policy.get_profile_tool_policy()` returns a frozen version, denied
names, explicit-scope flag and resolved profile identity. Invalid explicit policy
types, version or unknown fields fail closed. Schemas and dispatch share this
policy. Agent initialization freezes effective callable names, including the
underlying authorized Tool Search catalog. Nested worker bindings only intersect
permissions, and schema caches include profile/policy and active callable ceiling.
Config changes apply to a fresh agent; existing conversation schemas stay stable.
Direct registry calls without agent context read the current profile policy.

The configured `skills` toolset becomes exactly `skills_list` and `skill_view`.
Denied names are refused before request hooks, checkpoints, pending-file staging
and handlers, including inline memory/provider tools and Tool Search unwrapping.
An explicitly supplied `enabled_tools=[]` grants no tools. No private memory store,
provider, prefetch, prompt block or synthetic replacement is initialized when
memory is denied. Existing memory-off keys are retained for packaging.

API session chat/stream, Chat Completions stream/nonstream, Responses
stream/nonstream and Runs use shared parsing. Opted-in profiles require an explicit
nonempty, distinct list of configured, known ordinary toolsets. Capability toolsets
are not accepted in that list. Invalid requests return HTTP 400 before agent work.
Direct construction enforces strict validation for opted-in profiles; unrestricted
profiles keep their existing configured-scope intersection.

Let C be the approved configured ordinary registry. Atlas must send C plus skills
for chat/import, with web optional for import, and exactly skills for ownership or
query. Import/ownership/query capabilities arrive separately and retain their
existing identity and call ceilings. Ownership/query intersect down to skills plus
their authorized capability tools. Email remains separately authorized. Tainted
email turns preserve skills only if profile policy removes every non-safe-read
tool from that toolset. No new FileMaker operation or guessed MCP name is added.

## Preserved history and signed tests

- Tests-only first commit `7538eeb12` preserved the original 1,804-line addendum.
- Merge `557dc63fe` preserved accepted email baseline
  `8ff4b970bcd81b19faede56df0ad732be454c66c` and all authors/history.
- Original unfinished checkpoint `c4b3623c0` is preserved.
- Correction commit `df7c73155` contains only the original attacker's signed
  revision-4 bytes: 1,832 lines, SHA-256
  `8c210dede0a2a65415fa1040939e3b76be42f36ab78f5a98dea630ec84b7fc96`.
- Source candidate `0a1a75a12da87c92f2b43f9c2ba1f86b5dc6727b` completes the remaining
  release-root and safe email skill-read behavior, cache ceiling and deferred-call
  controls. Later changes in this checkpoint add only evidence/tests.
- Full history remains non-shallow. Accepted email is an ancestor; excluded d744
  is not. No Atlas file, deployment pin or accepted email guard was reverted.
- `git diff 8ff4b970bcd81b19faede56df0ad732be454c66c HEAD --stat` was run. No existing
  test was deleted or weakened relative to the accepted baseline. The addendum
  correction replaces six fixture lines and adds a positive control, exactly as
  authored/signed by the attacker; it is not a builder amendment.

## Local receipts

All Hermes commands use the repository's hermetic runner with at most two workers.
No installs, builds, paid/provider calls, live credentials/data or production
operations were used. The initial existing `/tmp/sno2018v` environment lacked
`requests`; those API checks were **not verified locally in that environment**.
The ignored `.venv` link was then pointed to existing `/tmp/sno4028_venv`.

1. `scripts/run_tests.sh -j 2 tests/gateway/test_profile_policy_and_scope_sno4027_attack.py tests/gateway/test_profile_policy_regressions.py tests/tools/test_skills_tool_profile_scope.py tests/tools/test_skills_tool.py tests/agent/test_prompt_builder.py -q`
   — **293 passed**, including all 36 signed cases and four complementary controls.
2. `scripts/run_tests.sh -j 2 tests/gateway/test_api_server_toolset.py tests/gateway/test_atlas_email_taint_runtime.py tests/gateway/test_atlas_email_main_integration_attack.py tests/gateway/test_atlas_email_tool.py tests/gateway/test_api_server_runs.py tests/tools/test_tool_search.py tests/agent/test_tool_dispatch_helpers.py tests/run_agent/test_memory_provider_init.py tests/agent/test_system_prompt.py tests/agent/test_prompt_caching.py -q`
   — **340 passed**. These include accepted email guards, worker propagation,
   ordinary-profile construction, memory, Tool Search and prompt-cache controls.
3. `scripts/run_tests.sh -j 1 tests/gateway/test_profile_policy_regressions.py -q`
   after adding the fresh-process control — **5 passed**. Two fresh Python
   processes each initialize a real offline agent, expose exactly the two skill
   reads, reject six management actions through function/registry/agent dispatch,
   reject memory invocation, preserve skill/pending trees across denied calls and
   preserve seeded private memory bytes without injecting them. Permitted skill
   reads retain the existing usage sidecar behavior.
4. Earlier focused external-directory, sync, guard and schema-cache checks passed.
   The broader seven-file API/email run had **438 passed, 3 failed**: ordinary
   constructor scope and explicit skill-root override regressions were fixed and
   their files passed in subsequent commands above. The remaining detailed-health
   test also fails on a disposable extraction of exact accepted baseline 8ff4b970b:
   `scripts/run_tests.sh -j 1 tests/gateway/test_api_server.py -k test_health_detailed_returns_ok -q`
   — **1 failed, 207 deselected**, expected `ok`, received `degraded`. Unrelated
   readiness behavior is unchanged.
5. `git diff --check` — clean. Ruff **not verified locally**:
   `.venv/bin/python -m ruff --version` reports `No module named ruff`; CI must run
   the repository's lint job. No check was skipped or weakened.

## Exact-candidate Atlas evidence and blocking fixture conflict

An unchanged Atlas packet was run from sibling SNO-3379 source, with a clean
environment and disposable HOME, against `git archive` of exact candidate
`0a1a75a12da87c92f2b43f9c2ba1f86b5dc6727b`:

```sh
ATLAS_HERMES_SOURCE_DIR="$candidate_archive" \
ATLAS_HERMES_GIT_REPO="$full_history_hermes_repo" \
PYTHONPATH="$atlas_source/apps/atlas-dashboard/backend" \
/tmp/sno4028_venv/bin/python -m pytest \
  apps/atlas-dashboard/backend/tests/test_hermes_profile_skills_sno3379_attack.py \
  -q --tb=short -o addopts= -p no:cacheprovider
```

Result: **5 failed, 4 passed**. Frozen Atlas SHA-256 remains
`1f91cf1bbd7ee88f13355dae1c3c3f5240abfa1fff0a6624ee73ec8712989550`.
This is candidate override evidence, never reviewed-pin proof or acceptance.

Four failures expose the still-unimplemented Atlas profile packaging (skills
toolset/root/marker, memory flags/policy and management denial). The fifth must be
handled separately: the workflow test creates `APIServerAdapter` using `__new__`
at line 1199, initializes selected attributes at 1200-1216, and calls `_run_agent`
at 1257 without `_tainted_session_ids`. The accepted email baseline already reads
that attribute unconditionally in `_session_is_mailbox_tainted` (baseline line
2024; candidate line 2040). It raises `AttributeError` before the intended scope
assertions. The helper's accepted behavior is unchanged by this companion.

No lazy-state compatibility workaround or frozen-test amendment was added. This
is the second frozen-test dispute on the build and requires PM routing to the
Chief PM under the builder protocol. The original attacker alone owns any test
correction and applicable sign-off/release. Publication is held on that decision.

## PM amendments applied and remaining ownership

PM's original release said: "First commit is the exact 1,804-line addendum test
file" and "Then merge accepted email baseline ... normally, preserve all
history/guards". Those actions remain preserved as 7538eeb12 and 557dc63fe.

The renewed release says: "Preserve `7538eeb12`, `557dc63fe` and WIP history" and
"Commit revision-4 bytes unchanged as a separate correction commit before further
implementation". The history and df7c73155 meet those instructions. It says
"finish the remaining behavior and evidence, then push the task branch/open one
draft PR with as-built". Remaining source behavior and focused evidence are
recorded above; publication has stopped on the newly established fixture conflict,
not an added PM checkpoint. "All three review stages and exact-head CI remain"
is preserved; none is claimed complete. "No ... live/paid operation, merge or
deploy" was respected.

SNO-3379 still owns Atlas broker/profile/released packaging and the eventual
compatible reviewed pin. SNO-3997 owns existing deploy/startup callers and SOUL;
PR404 owns rules. R1/SNO-4026 consumes this policy interface after this source
work. No second launcher, privileged helper or startup monkey patch was added.
Concrete live MCP inventory, installed permissions and compatible reviewed Atlas
pin remain unverified downstream release gates. Independent Argon review,
Security, Verifier and exact-head required CI are outstanding; no Hostile QA.
