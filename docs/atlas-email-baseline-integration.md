# Pinned Atlas email baseline integration

## Scope and lineage (acceptance 1)

This change integrates the exact required email baseline `ed3774867c07299b2178f18b936b1c06b46be0c4`
with accepted main `976e25fc671fb2a7fa2154f92bb0aef963eadc33` on `paperclip/SNO-3977`.
Their merge-base is `6301854252f8725772b24d7d4c546c45ea84190a`.
The first new commit is the unchanged tests-only commit `0160300e1`.
Merge commit `55e2a83d4` retains both parent histories and original authors.

The email prefix changes `gateway/platforms/api_server.py`, `model_tools.py`,
`run_agent.py`, `tools/atlas_email_tool.py`, `tools/atlas_ownership_tool.py`,
`tools/atlas_query_tool.py`, `tools/delegate_tool.py`, `tools/registry.py`,
`toolsets.py`, and the existing email tests. Main changes since the merge-base
are confined to ownership implementation/tests and gateway test import setup.
The only overlapping source file is `tools/atlas_ownership_tool.py`: main's R1
semantic ingress/refusal-envelope changes and email's token filtering touch
separate lines. Git's merge completed with no conflicts and no manual resolution.
At the initial integration, no existing test was deleted or weakened. The later `d74466816b7baf69e177527266729502bf01ea82`
commit is not an ancestor: email/import capabilities remain mutually exclusive,
and mailbox taint does not disable Atlas import.

Chose a normal two-parent merge over replaying commits because the directive
requires exact baseline ancestry and preserved history. Chose one shared session
option parser over a second copy because both endpoints must enforce the same
capability, scope and taint rules.

## Bounded implementation (acceptance 2)

- Both synchronous and streaming session endpoints use the same existing
  capability/scope/taint validation and pass the parsed options to `_run_agent`.
- Ownership and query turns keep their exclusive toolsets even if an internal
  caller also requests email authorization.
- The existing dispatch guard blocks the additional named external/composite
  toolsets and MCP-prefixed toolsets. The signed repair below replaces that
  deny-list enforcement with an allow-list, including configured MCP aliases.
- Batch pre-scan normalizes bridge target names, bounds nested wrappers, and
  fails closed for unreadable wrappers. Message identifiers must be single-line.
- Ownership rejects all four capability prefixes and all Atlas capability/taint
  field names. Import rejects capability tokens in forwarded arguments, decoded
  responses and validation details. Existing harmless import details remain usable.

No new model tool or business validator was added by the repairs. The baseline's
existing capability-gated email tool is retained. Enforcement occurs at dispatch
and agent creation; no mid-conversation schema/prompt rebuilding was added.

## Frozen tests and evidence (acceptance 2 and 3)

Signed attack: [SNO-3978](/SNO/issues/SNO-3978#document-attack), revision
`fd65613f-b52e-422d-8464-b378123aea4a`. Its original 738-line Python block is
`tests/gateway/test_atlas_email_main_integration_attack.py`, SHA-256
`85d51e7ec8548da48b8ffcd746d869e718f41d9b0dd12b3b61c2821662484d38`
at first commit `0160300e1`. The signed addendum below supersedes one control
and appends tests without rewriting that original commit.

Input shapes and tests:

- Malformed/combined capability and scope inputs on both session endpoints:
  `test_session_chat_endpoints_refuse_malformed_or_combined_capabilities`.
- Valid capabilities, taint-only turns and narrowing scope on both endpoints:
  `test_session_chat_endpoints_forward_valid_capabilities_taint_and_scope`.
- Fake registered external/MCP tools with local and untainted controls:
  `test_tainted_run_refuses_mcp_and_external_toolsets_while_preserving_local_control`.
- Configured external/composite/MCP toolset names with untainted and import controls:
  `test_create_agent_strips_mcp_composite_and_external_toolsets_when_mailbox_tainted`.
- Normalized, nested and unreadable bridge inputs with propose control:
  `test_batch_pre_scan_taints_whitespace_padded_and_malformed_bridge_reads`.
- Message identifier shape rejection with single-line control:
  `test_multiline_message_id_is_refused_before_callback`.
- Four synthetic capability prefixes in request/response/error data:
  `test_all_atlas_tools_scrub_every_capability_prefix_in_requests_and_responses`.
- Reserved authority field names:
  `test_ownership_proposal_refuses_all_atlas_capability_authority_keys`.
- Internal mixed authorization flags:
  `test_ownership_and_query_single_tool_boundaries_exclude_atlas_email`.
- Accepted R1 semantic proposal and refusal envelopes on a tainted turn:
  `test_main_sno2018_r1_ownership_contract_and_query_fixes_survive_email_integration`.

Local commands/results:

1. `sha256sum tests/gateway/test_atlas_email_main_integration_attack.py`: matches
   the required hash above; the extracted block has 738 lines.
2. `python3 -m py_compile gateway/platforms/api_server.py tools/atlas_email_tool.py tools/atlas_ownership_tool.py tools/atlas_import_tool.py`: exit 0.
3. `git diff --check`: exit 0.
4. `scripts/run_tests.sh -j 2 tests/gateway/test_atlas_email_main_integration_attack.py`:
   could not start, exit 1, no supported virtualenv found. **Not verified locally.**
5. `scripts/run_tests.sh -j 2 tests/gateway/test_atlas_email_main_integration_attack.py tests/gateway/test_atlas_email_tool.py tests/gateway/test_atlas_import_tool.py tests/gateway/test_atlas_ownership_tool.py tests/gateway/test_ownership_r1_contract.py tests/gateway/test_atlas_query_tool.py`:
   same startup failure. **Not verified locally.** No direct pytest or install substituted.
6. `ruff check gateway/platforms/api_server.py tools/atlas_email_tool.py tools/atlas_import_tool.py tools/atlas_ownership_tool.py tests/gateway/test_atlas_email_main_integration_attack.py`:
   command not found. **Not verified locally.**
7. `ty check gateway/platforms/api_server.py tools/atlas_email_tool.py tools/atlas_import_tool.py tools/atlas_ownership_tool.py`:
   command not found. **Not verified locally.**
8. Executed the unchanged shell `run` block from
   `.github/workflows/contributor-check.yml` against recorded `origin/main`:
   `All contributor emails are mapped in AUTHOR_MAP.` No attribution/check edits.
9. `git merge-base --is-ancestor` for both pinned sources: success; for excluded
   `d7446681`: not an ancestor. GitHub main read before publication still matched
   recorded `976e25fc`.

## Review, CI and rollback (acceptance 4 and 5)

The single draft PR carries this evidence. CI must run the existing checks,
including the gateway tests above, at the pushed head; no green CI is claimed here.
[Repository CI runs](https://github.com/aulbricht/hermes-agent/actions/workflows/ci.yml).
Code Review (Argon), Security and Verifier must accept that same head through the
three existing native stages. No Hostile QA or PM publication checkpoint is added.

Risk: capability propagation and dispatch policy affect sibling session/tool paths;
hermetic integration tests and independent security review are required. Rollback
requires an independently authorized revert of this integration and its repairs;
no history rewriting or automatic rollback is performed.

Atlas Chief PM must carry the accepted PR/head for Allen's per-merge docket go.
This assignment does not authorize marking ready, merging, deployment, live mail,
paid/model calls, production reads, CRM writes, Atlas Approvals, outreach code or a
pin update. Record the eventual GitHub merge commit before outreach work starts;
that future step retains its separate frozen 404-line fence.

## Signed persistence and history-replay repair

The controlling Security CHANGES-NEEDED head is `f162054964b8241d7b06beae80edec0d47fb8aa1`.
Accepted attack revision `a6a17e48-55a6-4b31-b366-444d81cee439` supplies two exact
edits, applied in new tests-only commit `388a3ded9`: replacement of the disputed
start-of-turn toolset control and the appended 1005-line defensive test block.
The amended full file SHA-256 is
`b6ef6261d61d1ea6fb043fa52dfbaed89fdaf3cc5597325b6a6085b9868c34b8`.

Chose the signed allow-list over extending the deny-list because unrecognized
plugins, MCP aliases and composites must be refused by default. Already-tainted
turns expose only the existing Atlas capability toolsets, todo and clarify.
Mid-turn enforcement permits the five signed read-only tools while refusing
persistence and egress; it leaves cached prompts and schemas intact. The legacy
`TAINT_BLOCKED_TOOLSETS` export remains for existing callers, with enforcement
independent of that diagnostic set. No defense-in-depth recommendation was deferred.

The inline agent executors and memory manager guard persistence paths that bypass
registry dispatch. Direct memory, skill and file writers check at entry. Session
search taints before retrieval, and batch pre-scan handles it without requiring an
email capability. Background review and external memory synchronization stop both
in the finalizer and their AIAgent entrypoints. The executor binds a fresh mutable
taint holder before creating/running the agent; existing thread context propagation
shares that holder with tool workers. Results record observed taint before reset.
Session replay includes ancestor/inactive history; response chaining retains the
observed flag even when a result omits tool calls. Both session result surfaces
return the flag. The independent `/v1/runs` approval-streaming path is also wired.

Signed input shapes and tests (acceptance 2):

- Direct/registry/agent memory writes, external memory providers, skill actions,
  file replacement/V4A patches, and read-only controls:
  `test_tainted_run_refuses_persistence_writes_across_all_dispatch_paths_while_preserving_read_only_controls`.
- Clean and tainted finalization with review/sync callbacks:
  `test_tainted_turn_suppresses_background_review_and_external_memory_sync`.
- Empty-capability batches, direct/registry/agent transcript reads and fresh-run control:
  `test_session_search_taints_batch_and_run_across_all_dispatch_paths`.
- Both session endpoints with a fake agent that omits the taint result field:
  `test_session_chat_and_stream_return_observed_mailbox_taint_in_turn_result`.
- Persisted read/propose histories, Chat Completions session replay and both
  Responses chaining forms, with authenticated clean controls:
  `test_replayed_session_history_and_responses_chaining_propagate_mailbox_taint_while_clean_sessions_keep_tools`.

Additional must-check tests live in `test_atlas_email_taint_runtime.py`:
executor/worker holder identity and mutation visibility; memory-disabled skill
review controls; direct background-entrypoint suppression; and real toolset
resolution under the reported host config. These tests make no provider calls.

### Reported host values and limits of evidence

The task's relayed read-only host report names `platform_toolsets.api_server` as
`[web, acre-filemaker]` and disables terminal, file, browser, computer_use,
code_execution, delegation, cronjob, tts, memory, session_search, image_gen,
video_gen, todo and clarify. There is no memory section, so memory and user-profile
loading default off. No host read or runtime check was performed by this builder.
Live parity remains **not verified**; the report is evidence of config, not runtime.

The config-resolution test uses these exact behavioral values with a disposable
MCP URL and no connection. Clean email creation retains web and the configured MCP
alias; tainted creation retains only atlas_email. File/memory/session_search remain
disabled; skills and kanban are absent. With memory review off and skill_manage
absent, finalization schedules no review. A clean skill-enabled control does
schedule skill review despite memory being off; tainted controls suppress both
review and external sync regardless of availability. Runtime execution of these
tests remains for CI and the Verifier, including the approved read-only host path.

### Rework accounting and amendments

Relative to accepted code-review head `f162054964b8241d7b06beae80edec0d47fb8aa1`,
no test is deleted. The signed attack's weakened/replaced assertions are the two expectations
that `file` stays enabled on already-tainted email/import turns. Authority:
current attack note: “take the addendum edits (including the in-place replacement
and the replayed-history test ... ) from `a6a17e48`”; signed addendum (d).6 and
(e) Edit 1 replace that whole function unchanged. No product change is reverted;
the deny-list implementation is superseded by signed addendum (d).1's allow-list.

PM amendments applied:

- Repair directive: “commit the signed addendum/amendments unchanged in a new
  tests-only commit before the repair” — commit `388a3ded9`, no history rewrite.
- Release: “using signed SNO-3978 revision a6a17e48 and both recorded must-checks” —
  frozen edits above and new executor/config/finalizer tests.
- Priority: “keep this email guardrail repair and all three existing review stages,
  below skills ... and Queries” — scope unchanged; no review stage removed.
- Dependency: “downstream companion does not replace those checks” — existing
  Code Review (Argon), Security, Verifier and new-head CI remain required.
- Latest: “No new scope, production/mail operation, paid call, merge or deploy” —
  all work uses local fixtures and development tooling only.

Local repair verification: canonical focused runner exits 1 before collection
(no supported virtualenv); therefore tests are **not verified locally**. Syntax,
whitespace, frozen-block equality and ancestry checks are recorded in the handoff.
Ruff and ty remain **not verified locally** because executables are unavailable.
Old-head CI/reviews do not accept this repair; draft PR #32 needs all checks and
three independent stages at the new exact head. No merge or deployment is authorized.

Repair static receipts: `python3 -m py_compile` on all 13 changed Python files
passed; `git diff --check` passed; exact signed-block and full-file SHA checks
passed. Both `git merge-base --is-ancestor ed3774867c07299b2178f18b936b1c06b46be0c4 HEAD`
and the same command for `976e25fc671fb2a7fa2154f92bb0aef963eadc33` exit 0;
the excluded `d74466816b7baf69e177527266729502bf01ea82` command exits 1 as required.
The unchanged contributor workflow's shell predicate exits 0:
“All contributor emails are mapped in AUTHOR_MAP.”

### CI failure repair

The exact-head check of `d98d90547e996e4da4f8efd1892fd1566a1a3bcf` found
two failures in [CI run 44511362836](https://github.com/aulbricht/hermes-agent/actions/runs/44511362836).
The frozen persistence test reached the sequential refusal branch, which set
`function_result` but omitted `tool_duration`; reporting the refused call raised
`UnboundLocalError` in `agent/tool_executor.py`. That branch now records elapsed
time like the other inline branches. The unchanged frozen test exercises two
sequential refusals (built-in and external memory) and asserts both results and
unchanged stores, covering the fix without an additional duplicate test.

The other failure was the baseline
`test_create_agent_drops_egress_toolsets_when_mailbox_tainted_even_with_scope`:
its old expected list included `file`. The assertion now expects only `todo`.
Authority: signed addendum (d).1 explicitly says start-of-turn filtering
“automatically strips `"file"`, `"memory"`, `"skills"`”; the PM repair directive
requires “persistence-write isolation”. This is the only additional replaced
baseline assertion; no test is deleted, skipped or weakened to admit writes.
The clean web control and capability-scoped email control remain intact. The
attacker-owned file remains byte-identical to revision `a6a17e48`, SHA-256
`b6ef6261d61d1ea6fb043fa52dfbaed89fdaf3cc5597325b6a6085b9868c34b8`.
No product change is reverted and no later PM amendment was received this wake.

`python3 -m py_compile agent/tool_executor.py tests/gateway/test_atlas_email_tool.py`
and `git diff --check` passed. The focused canonical command
`scripts/run_tests.sh tests/gateway/test_atlas_email_main_integration_attack.py tests/gateway/test_atlas_email_tool.py tests/gateway/test_atlas_email_taint_runtime.py -j 2`
still exits before collection because no supported virtualenv exists: **not
verified locally**. Ruff and ty remain unavailable. Both source ancestry checks
and exclusion of d744 remain required; required CI and all three independent
reviews must accept the new head.
