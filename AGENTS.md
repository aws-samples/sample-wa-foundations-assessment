# Repository Agent Guidelines

These instructions apply to the entire repository.

## Working Principles

- Preserve existing user changes and unrelated untracked files.
- Keep changes focused on the requested behavior.
- Treat source code and tests as the executable source of truth.
- Reconcile behavior changes with `README.md`, `.kiro/specs/`, and
  `docs/testing/test-cases.md`.
- Do not hand-edit generated files when a repository generator exists.
- Never include real credentials, account IDs, resource ARNs, or customer data
  in tests, fixtures, reports, logs, or documentation.

## CodeGraph

If a `.codegraph/` directory exists at the repository root, use CodeGraph
before grep, find, or broad file reading when locating or understanding code:

```bash
codegraph explore "<symbol names or question>"
```

Use the `codegraph_explore` MCP tool when available. Do not create or rebuild a
CodeGraph index; indexing is the user's decision. If `.codegraph/` is absent,
use `rg` and normal file inspection.

## Project Overview

WAFA is a Python assessment tool for AWS Organizations foundations. Its main
execution path is:

```text
discover account and Regions
  → run account-mode-appropriate checks
  → calculate maturity
  → render HTML, CSV, JSON, and console output
  → write locally or upload reports to S3
```

Important modules:

- `src/main.py`: orchestration and output
- `src/discovery/`: account, partition, and Region discovery
- `src/checks/`: assessment checks and maturity scoring
- `src/report/`: HTML and CSV generation
- `report-ui/`: Cloudscape React source for the HTML report bundle
- `deployment/wafa-stack.yaml`: deployed resources and execution permissions
- `deploy.sh`: source packaging, deployment, and initial-run workflow
- `run-local.sh`: local and CloudShell assessment workflow
- `scripts/generate_sample_report.py`: deterministic sample artifact generator
- `tests/unit/`: mocked unit and report tests

## Account and Region Control Flow

`src/discovery/account_type.py` classifies the current account by comparing the
STS caller account with `Organization.MasterAccountId`:

- Management account: run all organization and account checks.
- Member account: run the limited per-account assessment.
- Standalone account: run the same limited per-account assessment.

Organization existence and management-account identification checks run in
every mode. Management-only checks must be skipped in limited mode rather than
synthesized as failed results.

`detect_partition()` maps the configured Region to the `aws`, `aws-us-gov`, or
`aws-cn` partition and selects the appropriate default Region for global or
partition-scoped checks. Region discovery accepts the configured AWS Region,
calls EC2 `DescribeRegions`, and currently includes only
`opt-in-not-required` Regions. Preserve the partition-specific fallback lists
when discovery fails.

Per-account regional checks in `src/checks/account_resources.py` use
`ThreadPoolExecutor` for concurrent scanning. Preserve bounded concurrency,
Region-unavailable handling, and the distinction between:

- finding a resource in any Region
- confirming that a resource is absent from every inspected Region
- receiving an API error that prevents a reliable conclusion

## Supported Python Versions

- Runtime dependencies and local assessment usage support Python 3.9+.
- Development dependencies require Python 3.10+.
- CI and deployed CodeBuild execution use Python 3.12.

Prefer Python 3.12 for development and validation.

## Setup and Validation

Create a development environment:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
python -m pip install uv pip-audit==2.10.1 cfn-lint
python -m pip check
```

Before every push, run the complete local validation set below. Do not push
when any applicable check fails.

```bash
# Match CI's current, unpinned Ruff tool rather than relying only on the
# version pinned in requirements-dev.txt.
uv tool run ruff check --output-format=github src tests scripts
uv tool run ruff format --check src tests scripts

python -m compileall -q src tests scripts
bash -n deploy.sh run-local.sh

python -m pytest tests/unit/ \
  -v \
  --tb=short \
  --cov=src \
  --cov-report=term-missing \
  --cov-report=xml:coverage.xml

python -m pip_audit -r requirements-dev.txt
cfn-lint deployment/*.yaml

npx --yes markdownlint-cli2 README.md AGENTS.md
git diff --check

# When report-ui/ or src/report/assets/ changed:
(cd report-ui && npm ci && npm audit --audit-level=moderate && npm test \
  && npm run build) && git diff --exit-code -- src/report/assets/
```

When a container runtime (Docker or Finch) is available, also run the ASH
security scan before pushing:

```bash
python -m pip install \
  "git+https://github.com/awslabs/automated-security-helper.git@v3.7.1"
ash --mode container     # Add --oci-runner finch when using Finch
```

Run focused tests while iterating, but run the complete unit suite before
finishing a code change. The GitHub lint workflow runs the latest Ruff release,
which may enforce rules that are not present in the version pinned for local
development; the `uv tool run` commands above intentionally reproduce that
behavior.

## Security Scanning

[Automated Security Helper (ASH)](https://github.com/awslabs/automated-security-helper)
runs in two GitHub workflows:

- `ash-security-scan.yml`: files added or modified by a pull request
- `ash-full-repository-scan.yml`: the full repository on push to `main`,
  monthly, and on manual dispatch

Both fail on actionable findings at `MEDIUM` severity or higher.

- Keep ASH pinned to a released version. The version appears in both
  workflows' `ASH_VERSION`, in `README.md`, and in this file; update them
  together.
- Keep GitHub Actions in the ASH workflows pinned to full commit SHAs with a
  version comment.
- `.ash/.ash.yaml` is the single ASH configuration. Use `ignore_paths` only for
  generated, vendored, or untracked content; use `suppressions` for reviewed
  findings in tracked source.
- Every suppression must name a specific `rule_id` and `path`, give a concrete
  `reason`, and use an `expiration` date when the exception is temporary.
- Fix real findings rather than suppressing them. Do not suppress findings just
  to make CI pass without the user's approval.
- `.ash/ash_output/` is local scan output and must not be committed.

## AWS Safety and Scope

- Do not call live AWS APIs unless the user explicitly authorizes a live run.
- Unit tests must use mocks or Moto and must not require AWS credentials.
- Assessment checks may use read, list, describe, and get operations only.
- Do not add create, update, delete, put, start, stop, invoke, or other mutating
  operations to assessment checks.
- The deployed execution path is not completely write-free: it writes reports
  to the WAFA results bucket, writes service logs, and may publish to the
  configured WAFA SNS topic.
- Keep write permissions scoped to WAFA-owned resources.
- If a new check calls another AWS API, update the assessment policy in
  `deployment/wafa-stack.yaml` with the narrowest required read permission.

Running `deploy.sh` creates real AWS resources and may incur cost. Before a live
deployment, determine whether the selected stack and source bucket already
exist. Clean up only resources created for the requested test.

## Check Contract

Every pass/fail check returns a flat dictionary containing:

- `check`: stable, unique display name
- `description`: concise explanation
- `status`: `complete`, `incomplete`, or `error`
- `required`: boolean
- `weight`: integer used to order recommended next steps
- `loe`: level-of-effort integer
- `remediationLink`: authoritative remediation documentation
- `error`: included only when status is `error`

Check requirements:

- Catch API and evaluation failures and return `status: "error"`. Broad
  `except Exception` handlers are intentional here; `ruff.toml` scopes the
  BLE001 exemption to `src/checks/` and `src/discovery/` only.
- Do not crash the assessment because one check failed.
- Distinguish an observed absence (`incomplete`) from an inability to determine
  state (`error`).
- Paginate list operations where the AWS API supports pagination.
- Preserve partition and Region behavior.
- Keep account-mode gating explicit.
- Use exact, stable check names because maturity scoring and capability axes
  join results by name.

`delegated_admin.py` is informational and intentionally does not use the
pass/fail check shape.

## Adding or Changing a Check

When adding, removing, or renaming a check:

1. Update the relevant module under `src/checks/`.
2. Update orchestration or account-mode gating in `src/main.py` if needed.
3. Update IAM read permissions in `deployment/wafa-stack.yaml`.
4. Update maturity criteria in `src/checks/maturity.py`.
5. Update `CAPABILITY_AXES` in `src/report/html_report.py`.
6. Add complete, incomplete, and error-path tests.
7. Reconcile the FR mapping in `.kiro/specs/`.
8. Update `README.md` and `docs/testing/test-cases.md`.
9. Regenerate tracked sample reports.

Do not silently reuse or change the meaning of an existing check name.

## Maturity Scoring

`src/checks/maturity.py` is the single source of truth for:

- maturity level names and descriptions
- level criteria
- next-level progress
- recommended next steps
- the structured scoring explanation rendered in the HTML report

The maturity level is the highest sequential level whose criteria are
complete. It is not a weighted average.

Weights only order recommended next steps. They do not change the level or its
progress percentage.

Keep each criterion unique within a level. In particular, Level 4 combines its
additional controls with organization-service criteria through
`_LEVEL_4_CRITERIA`; do not duplicate service checks in both lists.

Level 5 requires every returned check to be complete. Both `incomplete` and
`error` results block Level 5.

When changing scoring:

- update scoring unit tests
- update HTML report tests
- verify the structured `scoring_model`
- regenerate the sample HTML and JSON
- update the README test count if the suite size changed

Do not duplicate maturity criteria directly in the HTML template. The report
must render the structured data returned by the scoring module.

## HTML Report

The generated report must remain a self-contained HTML document:

- no CDN or runtime network dependency
- real Cloudscape Design System components, not hand-written look-alikes
- inline Cloudscape JavaScript and CSS bundle from `src/report/assets/`
- assessment data embedded as JSON with Jinja2's `tojson` filter
- Jinja2 sandbox with autoescaping for the HTML shell
- responsive light and dark themes through Cloudscape `applyMode`
- no trailing whitespace in generated output

The UI source lives in `report-ui/` (React, Cloudscape, esbuild, Vitest).
`src/report/assets/report-ui.js` and `report-ui.css` are generated artifacts:
do not hand-edit them. After changing `report-ui/`, run `npm ci`, `npm test`,
and `npm run build` in `report-ui/`, commit the rebuilt assets, and regenerate
the sample reports. The build escapes `</script` and `</style` and fails on
remote resource URLs or trailing whitespace. Keep npm dependencies pinned
through `package-lock.json`.

Use `safe` only for markup generated entirely by trusted repository code, such
as the prebuilt report UI bundle. Never mark AWS-provided or user-controlled
text safe; pass it to the UI through the `tojson` payload, where React renders
it as text.

The report should clearly distinguish:

- current maturity level
- achieved levels
- next target
- later levels
- complete, incomplete, error, and not-assessed criteria
- provisional maturity for non-management accounts

`CAPABILITY_AXES` maps capability names to exact check-name strings and feeds
the Cloudscape capability coverage bar chart. A check may
contribute to more than one axis. Update this mapping whenever a relevant check
is added, removed, or renamed.

The Networking and Connectivity axis intentionally has no Phase 1 checks and
therefore reports 0% coverage. Do not silently remove it or assign unrelated
checks merely to avoid the visible gap.

Raw JSON is assembled in `src/main.py`, not in the report package. If the
assessment result schema changes, update the JSON assembly, report tests,
sample artifacts, and documentation together.

## Sample Reports

Files under `sample-reports/` are generated artifacts. Regenerate them with:

```bash
python -m scripts.generate_sample_report
```

The generator must:

- call the production orchestrator and report code
- mock AWS at the `boto3.client` seams
- use obvious placeholder identifiers only
- exercise complete, incomplete, and error states
- remain deterministic

If `report-ui/` changed, rebuild `src/report/assets/` before regenerating. The
generator stops if the UI assets are missing and, after generating, fails
unless:

- the HTML inlines the current UI bundle and embeds the report data
- the HTML report data, CSV, and JSON are mutually consistent
- the JSON includes a five-level `scoring_model`
- complete, incomplete, and error states are all present
- the HTML and JSON have no trailing whitespace

Still review manually that no real identifiers or credentials appear and that
the report renders its maturity ladder and detailed criteria in a browser.
`sample-reports/*.csv` uses CRLF line endings; `.gitattributes` exempts them
from `git diff --check`.

Do not manually edit the generated HTML, CSV, or JSON.

## Deployment Details

`deploy.sh` packages only:

- `src/`, including the committed report UI bundle in `src/report/assets/`
- `requirements.txt`
- `buildspec.yml`

The temporary local archive is:

```text
/tmp/wa-foundations-source-<account-id>.zip
```

It is uploaded to:

```text
s3://wa-foundations-source-<account-id>-<region>/wa-foundations-source.zip
```

The `AutoStartBuild` parameter defaults to `true`, so direct CloudFormation
stack creation uses the custom resource to start the first build. `deploy.sh`
sets the parameter to `false`, tolerates an empty change set, starts one build
itself, and waits on the exact build ID returned by CodeBuild.

The source bucket is outside the CloudFormation stack and must remain available
for CodeBuild reruns. The results bucket is created by CloudFormation and has
versioning enabled.

For teardown, permanently delete every results-bucket object version and delete
marker before deleting the stack. Delete the source bucket separately after the
stack is gone.

## Documentation

- Keep commands copyable and platform assumptions explicit.
- Qualify the difference between read-only assessment calls and deployment or
  report-write behavior.
- Do not describe WAFA as scanning resources in every member account.
- Describe the maturity model as WAFA's project-defined assessment model, not
  an official AWS Well-Architected Tool score.
- If tests are added or removed, update the README badge and documented count.
- Use authoritative AWS documentation for remediation links.

## Git and Generated Changes

- Preserve unrelated worktree changes.
- Avoid repository-wide formatting unless requested or required by the change.
- Review generated diffs for accidental identifiers and nondeterminism.
- Run `git diff --check` before finishing.
- Do not commit, push, create a branch, or open a pull request unless requested.
