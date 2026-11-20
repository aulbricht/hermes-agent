# Read-only skills and workflow scopes: implementation checkpoint

Source work for SNO-4027. Not accepted, pinned or deployed. The Chief PM directed
the same builder to resume requested review changes from checkpoint `0c1d77b80`
in comment `dbcb050a-cd64-48f5-a7f3-566599e6bc95`. The code roles are capability/privacy
guardrails and platform plumbing; no workflow judgment or new model tool is introduced.
The earlier fixture dispute and test receipts below are historical evidence.

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


## Requested review repairs after checkpoint 0c1d77b80

The Chief PM's latest direction is: "Builder: please resume the requested review
changes from the preserved checkpoint." This supersedes the earlier source hold
for these repairs. The three configured reviews, exact-head CI, frozen test
ownership, and downstream packaging/pin gates remain. No attack stage was reset.

- Finding 2 / AC2: legacy flat markdown lookup now uses `iter_skill_index_files`,
  the same excluded-directory and release-root traversal as normal skill discovery.
  Complementary tests place same-named markdown under `.git`, `node_modules/pkg`
  and `.venv`, assert that the released skill loads, and preserve permitted nested
  legacy markdown as a positive control.
- Finding 3 / AC2: slash discovery uses approved roots in external-only profiles.
  Ordinary profiles retain local-first discovery and explicit directory overrides.
  A parameterized control checks both configurations against actual temporary
  local/released skill files.
- Finding 4 / AC5: removed the duplicate session scope check. The shared validator
  still runs first and retains list/type/length/distinct/configured/known checks.
  The unchanged signed endpoint cases cover rejection before agent work.
- Finding 1 / AC7-8: draft publication and exact-head CI are still pending at this
  local record. SNO-3781 revision 6 `a5e9c78f-d703-43c7-8c2c-9090d91e9738` has Security
  sign-off in comment `48c47bf8-00b4-44d6-874d-d5f611c1da78`. Its one setup line belongs
  to the Atlas packet; this Hermes branch does not edit Atlas files. Security's
  recorded candidate result remains 5 failed / 4 passed, now reaching the scope
  assertion; packaging is owned by SNO-3379. This is not released-pin proof.

Current-run commands and outputs:

```text
scripts/run_tests.sh -j 1 tests/gateway/test_profile_policy_regressions.py -k 'legacy_skill_lookup or slash_discovery' -q
error: no virtualenv found (the existing .venv -> /tmp/sno4028_venv is dangling)
.venv/bin/python -m ruff --version
No such file or directory
python3 -m py_compile agent/skill_commands.py tools/skills_tool.py gateway/platforms/api_server.py tests/gateway/test_profile_policy_regressions.py
exit 0
git diff --check
exit 0
sha256sum tests/gateway/test_profile_policy_and_scope_sno4027_attack.py
8c210dede0a2a65415fa1040939e3b76be42f36ab78f5a98dea630ec84b7fc96
```

The new pytest cases and Ruff are **not verified locally** in this run. No installs
or heavy suites were run. Required CI must execute the focused and unchanged
signed Hermes tests. No cross-repository rerun was possible without the test
environment; the previous and Security-attributed receipts are not new results.

Rework accounting: `git diff 0c1d77b8048c6e61abfb2c90bcae0ec37cfcbec7 HEAD --stat`
is the delivery comparison from the last reviewed checkpoint. No deleted or
weakened tests, no reverted accepted source changes, and no frozen test edits.
The duplicate scope block is removed as finding 4 requests; the earlier shared
validator retains its behavior. Prior PM amendments are quoted and mapped above;
the latest resume direction is met by these repairs. Exact-head CI and independent
reviews remain incomplete until their actual receipts exist.


## Same-PR CI repair at 53d70e1a

PM released this repair in comments `bdee343e-6243-4aeb-8d9e-8f886f232e20`
and `c9cacdaa-0b30-4e8f-86a3-68c63816c49f`. Their direction supersedes the
historical publication hold above. Workflow 44731204175 failed at exact head
53d70e1a03a217d113b59b0894de5d665dd5abda; the four failing files were traced:

- `tests/tools/test_code_execution.py`: two old assertions required empty or
  nonoverlapping enabled names to grant every sandbox tool. Those expectations
  conflict with the adopted directive, "Enforce supplied enabled_tools membership,
  treating [] as empty." Replaced them with ImportError and zero-dispatch
  assertions. The None/default positive control and subset tests remain.
- `tests/run_agent/test_run_agent.py`: seventeen failures came from the fixture
  advertising only web_search, then exercising unadvertised memory, browser_type,
  todo or write_file calls, or mutating a frozen callable set. The fixture now
  advertises those tools at construction; three obsolete post-construction add
  calls are removed. Every existing behavior assertion remains unchanged.
- `tests/run_agent/test_token_persistence_non_cli.py`: the lazy database test
  constructed an agent advertising zero tools. Only that caller now advertises
  session_search before initialization; token persistence callers still have no
  tools, and all existing assertions remain.
- `tests/gateway/test_atlas_ownership_tool.py`: twenty-four failures exposed the
  unrestricted profile's writable skills toolset surviving ownership narrowing.
  Ownership and query now use the same read-only-skill predicate as email taint:
  retain requested/configured skills only when policy removes every unsafe tool.
  This preserves the default capability-only boundary and the opted-in contract.
  That entire test file stays unchanged. Six added controls cover both workflows
  with writable, read-only and fully denied skill inventories. Signed real-agent
  workflow cases remain unchanged and cover actual callable schemas/dispatch.

Choice: fix fixture construction instead of allowing unadvertised calls, because
per-agent callable ceilings are immutable. Preserve the old ordinary capability
boundary instead of granting skill_manage during ownership/query. Flag either
choice for independent review if a different contract is intended.

Current repair validation:

```text
scripts/run_tests.sh -j 2 tests/run_agent/test_run_agent.py tests/run_agent/test_token_persistence_non_cli.py tests/tools/test_code_execution.py tests/gateway/test_atlas_ownership_tool.py tests/gateway/test_profile_policy_regressions.py tests/gateway/test_profile_policy_and_scope_sno4027_attack.py -q
error: no virtualenv found (.venv points to missing /tmp/sno4028_venv)
.venv/bin/python -m ruff check gateway/platforms/api_server.py tests/run_agent/test_run_agent.py tests/run_agent/test_token_persistence_non_cli.py tests/tools/test_code_execution.py tests/gateway/test_profile_policy_regressions.py
No such file or directory
python3 -m py_compile gateway/platforms/api_server.py tests/run_agent/test_run_agent.py tests/run_agent/test_token_persistence_non_cli.py tests/tools/test_code_execution.py tests/gateway/test_profile_policy_regressions.py
exit 0
git diff --check
exit 0
sha256sum tests/gateway/test_profile_policy_and_scope_sno4027_attack.py
8c210dede0a2a65415fa1040939e3b76be42f36ab78f5a98dea630ec84b7fc96
```

Pytest and Ruff are **not verified locally** for this repair. No install, heavy
suite or live/provider operation ran. Required CI must run the four failure files,
new controls, frozen packet and sibling email/query paths before acceptance.

Rework accounting uses `git diff 0c1d77b8048c6e61abfb2c90bcae0ec37cfcbec7 HEAD --stat`
(last reviewed checkpoint) and `git diff 53d70e1a HEAD --stat` (CI repair delta).
No deleted tests, weakened assertions, signed-packet edits or reverted accepted
source changes. Two sandbox allowance expectations are explicitly replaced with
stricter refusal expectations as authorized by the directive quoted above and
PM's "distinguish obsolete expectations from regressions using the adopted
contract". Three fixture mutations are retired under the immutable-ceiling
contract; their tests and assertions remain. Existing email and ownership tests
are unchanged.

Latest PM amendments: "Trace and repair all failures and sibling paths" is met by
the four-file diagnosis and shared ownership/query/email predicate; runtime
success remains for CI to establish. "Continue the existing branch/PR, update
same-PR as-built, push the repair" is met by this additive repair on PR 35.
"Preserve frozen tests and security ceilings" is met by unchanged signed bytes,
no broadened production permissions and fixture-only setup repairs. "All three
stages were retained; final acceptance requires their verdicts and exact-head
green CI" remains a gate, not a completion claim. No re-attack or extra PM
checkpoint is introduced. Atlas packaging, original fixture installation and
compatible reviewed pin stay with SNO-3379; source acceptance does not depend
cyclically on that downstream pin.
