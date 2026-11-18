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
No existing test is deleted or weakened. The later `d74466816b7baf69e177527266729502bf01ea82`
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
  toolsets and MCP-prefixed toolsets. Tainted agent creation also removes
  configured MCP server aliases using the existing config resolver.
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
`85d51e7ec8548da48b8ffcd746d869e718f41d9b0dd12b3b61c2821662484d38`, unchanged.

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
