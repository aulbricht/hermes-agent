# Atlas parcels companion contract

This companion extends the existing `atlas_ownership` tool. It sends the exact
address or coordinates, radius, unit and optional centre tag to the fixed Atlas
callback. Atlas alone converts units, enforces the converted-radius cap, checks
the turn and reserves provider calls. Lightbox facts remain untrusted data.

## Source and dependency inputs

- Hermes base: `976e25fc671fb2a7fa2154f92bb0aef963eadc33`. The candidate Hermes
  revision must be recorded alongside the Atlas revision printed by the contract.
- Atlas fixture: `aulbricht/acre-atlas` at
  `fdd486b6e03b19a5138f1fa4278883425ba838d9`, mounted at `.contract/acre-atlas`.
  This is an offline source fixture, never an installed service or live database.
- Python 3.12 (joint constraint `>=3.12,<3.14`).
- Hermes `pyproject.toml` SHA-256:
  `d1eee316b301dbaa35d8e59ab138cf86a4937f0fe4c65f97d839d798ae6a6755`.
- Hermes `uv.lock` SHA-256:
  `8d03d04a404c641e1c9642f0482e2d8752c57da02da94d612a5f30883b25fbca`.
- Atlas backend `pyproject.toml` SHA-256:
  `9fcfdd6a5822b66418c7ca504f26ac1a661ef6d5f57c8b43e98f29ae55b28c80`.
- Atlas backend `uv.lock` SHA-256:
  `dc12dd252c81446705e68638a417fb78feacbf03464297a7789043cec2378d81`.

In a clean CI environment with an already authorized Atlas source mount, verify
its revision and manifests, then provision the signed dependency route:

```bash
test "$(git -C .contract/acre-atlas rev-parse HEAD)" = fdd486b6e03b19a5138f1fa4278883425ba838d9
sha256sum pyproject.toml uv.lock \
  .contract/acre-atlas/apps/atlas-dashboard/backend/pyproject.toml \
  .contract/acre-atlas/apps/atlas-dashboard/backend/uv.lock
uv sync --locked --python 3.12 --extra all --extra dev --extra bedrock --extra wecom
printf '%s\n' 'pypdf==6.20.1 --hash=sha256:aa5a55ddcffdc5e5ab291d5decb23f6383f4e56f8e3263dc39af41fff03885ad' > /tmp/atlas-parcels-pypdf.txt
uv pip install --python .venv/bin/python --require-hashes --no-deps -r /tmp/atlas-parcels-pypdf.txt
scripts/run_tests.sh tests/gateway/test_atlas_ownership_parcels_attack.py \
  tests/gateway/test_atlas_ownership_parcels_schema.py \
  tests/gateway/test_atlas_ownership_parcels_contract.py \
  tests/gateway/test_atlas_ownership_tool.py -j 1 -s --tb=short
```

Compare all four manifest hashes to the values above before installing. The
additional wheel hash comes from the pinned Atlas lock; no live credentials,
model calls or provider requests are needed. The contract runs the actual
mounted ASGI callback, real capability and disposable SQLite database. Only
Lightbox HTTP is stubbed; CRM and Approval creation are tripwires.

## Current evidence and remaining dependencies

The signed tests are unchanged from the accepted attack:

- `test_atlas_ownership_parcels_attack.py`: SHA-256
  `2a02f0032980d750c3ead5033a054857b220f8545e055f2caf6ba14bbcf93bb6`.
- `test_atlas_ownership_parcels_contract.py`: SHA-256
  `57f0f3b071207dcdd54c857d57085edbc4467d6a31ae04c8054e10386868de98`.

A local canonical-runner execution using an existing Python 3.12 environment
and the exact local Atlas fixture produced **132 attack tests passed** and
**9 mounted contract tests passed, 4 failed**. Eight additional operation-coupling
discovery tests also passed. The environment was reused,
not provisioned from the locks, so this is runtime evidence rather than a
clean dependency-install receipt. The gateway control file produced 399 passes
and 30 failures; direct import of `gateway.run` identifies missing `requests`
in that reused environment. Those gateway paths are not verified locally.

The four mounted failures are the two documented success shapes, Stop between
geocoding and Parcels, and the budget boundary after geocoding. They return
`LIGHTBOX_RESPONSE_INVALID` at the pinned Atlas implementation: it expects
`_metadata` instead of documented `$metadata` and flat geocoding fields instead
of the documented nested location/metadata. The Atlas schema amendment tracked
by SNO-3384/SNO-3829 remains a prerequisite. Preserve these failing tests; do not
normalize provider shapes in Hermes or edit the Atlas fixture to pass them.
The nine passing cases cover provider denial, timeout, Stop before the callback,
and Atlas-owned converted-radius rejection.

Standard CI discovers this contract in `tests/gateway`, but
`.github/workflows/tests.yml` currently provisions Python 3.11 and no Atlas
checkout or extra backend dependencies. The source repository is private.
A CI-authorized source mount plus Python 3.12 and the locked dependencies above
are still required. No new credential or CI permission is added by this PR;
no skip or expected-failure marker hides the missing provisioning. The clean
CI contract remains unverified until that dependency is supplied.

## Pairing and release

The independently accepted Hermes head must be selected by a separately
reviewed Atlas `deploy/versions.txt` change after the Atlas schema amendment
and the cross-repository contract pass. This Hermes change does not edit that
pin, approve trial entitlement, merge, deploy, or establish usable completion
of SNO-3384. Code Review, Security and Verifier must identify the final head.
