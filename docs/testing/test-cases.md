# WAFA Test Cases

This is a shared test plan for validating WAFA (Well-Architected Foundations
Assessment).
Grouped by area so multiple testers can claim sections in parallel. Each case has
a stable ID, preconditions, steps, and expected result.

## How to use this doc

This file is the **test spec** — treat it as read-only. Do not record results here.
Instead:

- Claim a section (put your alias next to the group heading) so testers don't overlap.
- Run each case and note Pass / Fail / Blocked for yourself.
- **For every Fail (or Blocked), open a Bug report issue** using the repo's
  `Bug report` issue template and fill in the details there. This keeps failures
  tracked and triageable instead of scattered in a doc.
  - **Put the test case ID in the issue title**, e.g. `[Bug]: A3 — second-region deploy fails source download`.
  - Passes don't need an issue (optionally note them wherever your team tracks a run summary).

### What to include in the bug issue (maps to the template fields)

- **Affected component** and, if relevant, the **Check name / FR-#**.
- **Account mode** (Management / Member / Standalone) and **Run mode**
  (Deployed stack / Local / CloudShell).
- **Version or commit** you tested (`git rev-parse --short HEAD`).
- **AWS partition and Region(s)** — names only.
- **Steps to reproduce**, **Actual result**, **Expected result**.
- **Sanitized logs / report excerpt** — for deploy/CodeBuild failures include the
  CodeBuild build status and the failed phase message; for report issues include
  the browser + version and a screenshot.

### Sensitive data — do not paste

Issues are visible to others. **Never include** account IDs, ARNs, credentials,
customer data, or unredacted logs. Redact identifiers before pasting. Suspected
vulnerabilities go through the repo **Security** tab, not a normal issue.

## Scanning scope (read this first — avoids the #1 misconception)

**The region you deploy into does NOT limit what gets scanned.** WAFA discovers
and scans **all enabled regions in the account** (default-enabled plus opted-in),
regardless of where the CodeBuild job runs.

- Region discovery (`describe_regions` with `opt-in-status` in
  `opt-in-not-required`, `opted-in`) returns every enabled region in the account. The deploy region is only
  the region used to *make that one API call* — it does not filter the results.
- The per-account resource checks (CloudTrail, Config, EC2, VPC) then fan out
  across every region in that list, in parallel.

| The deploy region controls | The deploy region does NOT control |
|---|---|
| Where the CodeBuild project runs | Which regions get scanned |
| Where the S3 source/results buckets live | Assessment coverage (always account-wide) |
| The partition default (us-gov / cn) | — |

**Intentional exclusions (not bugs, but confirm they match expectations):**
- **Opt-in regions that are NOT enabled are skipped.** Opt-in regions the account
  has enabled (e.g. ap-east-1, me-south-1) ARE scanned. See test case D4.
- **A few calls are deliberately partition-pinned:** IAM `list_users` uses the
  partition default region (us-east-1, us-gov-west-1, or cn-north-1);
  Organizations/STS are region-agnostic control-plane APIs; Control Tower
  `GetLandingZone` is re-pointed to the landing zone's home region (parsed from
  its ARN), and `ListLandingZones` is retried in opted-in regions when the
  default-region call finds nothing or fails.
- **A region that cannot be inspected does not hide a match elsewhere.** If an SCP
  denies one region, a trail or recorder found in another region still reports
  complete. If nothing is found and some region was denied, the check reports
  `error` naming the denied regions. A region whose SDK client cannot be
  created is treated the same way. See test case D7.

**So when a report is missing a region's resources, it's one of:** (a) that region
is an opt-in region that is not enabled, (b) the account genuinely has nothing there, or
(c) a deploy failure meant no report was produced at all (see A3) — NOT the scan
being scoped to the deploy region. Test case D3 proves the multi-region scan works.

## Environment matrix (what "coverage" means here)

| Dimension | Values to cover |
|---|---|
| Account type | Management, Member, Standalone (not in an org) |
| Deploy method | `deploy.sh` (CloudFormation + CodeBuild), `run-local.sh` (local or CloudShell) |
| Region | us-east-1, one other commercial region (e.g. eu-west-1), an opt-in region (e.g. ap-south-2) |
| Partition | Commercial (`aws`); GovCloud / China if you have access |
| Output target | Local dir, `s3://` URI |
| Re-run | First deploy vs. second deploy (same account, different region) |

---

## Group A — Deployment via deploy.sh (CloudFormation + CodeBuild)

Owner: ____

### A1. Fresh deploy in us-east-1 (baseline happy path)
- **Precondition:** Management account, no existing assessment source bucket.
- **Steps:** `./deploy.sh --profile <mgmt> --region us-east-1`
- **Expected:** Stack creates; CodeBuild `SUCCEEDED`; four files land in
  `s3://<results-bucket>/<account-id>/` (wafa-report.html, wafa-checks.csv,
  wafa-raw.json, wafa-org-map.csv). A member or standalone run also writes
  the map CSV, holding only a `Not available` row with the reason.

### A2. Fresh deploy in a non-us-east-1 region (validates the s3 mb flag fix)
- **Precondition:** Clean account (no prior assessment source bucket), OR fully
  torn down.
- **Steps:** `./deploy.sh --profile <mgmt> --region eu-west-1`
- **Expected:** Stack creates; source bucket is created **in eu-west-1**; CodeBuild
  `SUCCEEDED`; reports present.
- **Why:** Regression guard for `fix(deploy): Fix bucket creation outside us-east-1`.

### A3. Second-region deploy in an account that already deployed to us-east-1
- **Precondition:** A2/A1 already ran in this account (bucket
  `wa-foundations-source-<acct>-<region>` exists).
- **Steps:**
  1. `./deploy.sh --profile <mgmt> --region us-east-1` (wait for finish)
  2. `./deploy.sh --profile <mgmt> --region eu-west-1`
- **Expected:** Second deploy's CodeBuild `SUCCEEDED`; its Region-specific
  source bucket is created and reports are present.
- **Diagnostics to capture:**
  - `aws s3api get-bucket-location --bucket wa-foundations-source-<acct>-<region>`
  - `aws codebuild batch-get-builds --ids <build-id>` (failed phase message)
- **Why:** Regression guard confirming that the Region-specific source-bucket
  name prevents a cross-Region source fetch.

### A4. Deploy with email notification
- **Steps:** `./deploy.sh --profile <mgmt> --region us-east-1 --email <you>@example.com`
- **Expected:** SNS topic + subscription created; confirm the SNS subscription email;
  on build completion an email arrives referencing `s3://<results-bucket>/<account-id>/`.

### A5. Deploy without email (SNS skipped)
- **Steps:** `./deploy.sh --profile <mgmt> --region us-east-1` (no `--email`)
- **Expected:** No SNS topic created (CreateSNSTopic condition false); build still succeeds;
  post_build SNS publish is skipped (SNS_TOPIC_ARN empty).

### A6. Custom stack name
- **Steps:** `./deploy.sh --profile <mgmt> --region us-east-1 --stack-name wa-foundations-test-1`
- **Expected:** CodeBuild project named `wa-foundations-test-1`; IAM role named
  `wa-foundations-test-1-codebuild-role`; results bucket name starts with
  `wa-foundations-results-`.

### A7. Missing required --profile
- **Steps:** `./deploy.sh --region us-east-1`
- **Expected:** Exits with `ERROR: --profile is required`, non-zero exit code.

### A8. Re-run assessment without redeploying
- **Precondition:** Stack already deployed.
- **Steps:** `aws codebuild start-build --project-name <stack> --profile <p> --region <r>`
- **Expected:** New build runs; reports overwritten in `s3://<results-bucket>/<account-id>/`.

### A8a. Redeploy with an unchanged template

- **Precondition:** Stack already deployed; change only files under `src/`.
- **Steps:** `./deploy.sh --profile <mgmt> --region us-east-1`
- **Expected:** CloudFormation reports no changes without failing the script;
  the script uploads the new source, starts a new build, prints its build ID,
  and waits on that build (not an earlier one). The new report reflects the
  updated source.

### A8b. Concurrent unrelated build during deployment

- **Precondition:** Stack already deployed.
- **Steps:** Start `deploy.sh`, then manually start the same CodeBuild project
  while CloudFormation deployment is in progress.
- **Expected:** The script starts its own build after CloudFormation completes
  and waits on the exact ID returned by that `StartBuild` call. The unrelated
  build's status does not determine deployment success.

### A9. Teardown
- **Steps:** Follow the README "Tear Down" commands: permanently delete every results-bucket
  object version and delete marker, delete the stack, wait for deletion, then
  `aws s3 rb s3://wa-foundations-source-<acct>-<region> --force`.
- **Expected:** Stack deletes cleanly (custom-resource Delete is a no-op) and both buckets are gone.
- **Watch for:** Running `delete-stack` without emptying the versioned results bucket first leaves
  the stack in `DELETE_FAILED`; `deploy.sh` prints a pointer to the README procedure, not a one-liner.

### A10. Failed build notification
- **Steps:** Deploy with `--email`, then make the build fail (for example, remove the assessment
  role's read permissions so every check errors).
- **Expected:** The build is marked FAILED (every check errored, so the run exits non-zero) and the
  SNS email subject is "Foundations Assessment FAILED" with the build ID, not "Complete".

---

## Group B — CloudShell / local run-local.sh

Owner: ____

### B1. run-local.sh from CloudShell in the management account
- **Steps:** clone repo, `./run-local.sh`
- **Expected:** Deps install; assessment runs; four report files written to the current
  dir, including `wafa-org-map.csv`. From a member or standalone account the map CSV
  holds only a `Not available` row with the reason.

### B2. Local run with explicit S3 output
- **Steps:** `python -m src.main --output s3://<some-bucket>/wafa/`
- **Expected:** Reports uploaded under `s3://<some-bucket>/wafa/<account-id>/`.

### B3. Local run with default output
- **Steps:** `python -m src.main` (no `--output`)
- **Expected:** Writes to `.` (current directory).

### B4. Run from CloudShell in a non-us-east-1 CloudShell region
- **Steps:** Switch CloudShell region, `./run-local.sh`
- **Expected:** Region discovery still enumerates all default-enabled regions; assessment
  completes (the assessment is region-agnostic; only IAM + region bootstrap are pinned).

---

## Group C — Account-mode behavior

Owner: ____

### C1. Management account = FULL assessment
- **Expected:** ~35 checks present (org governance FR-3, org services FR-4, Control Tower FR-5,
  identity FR-6 both checks, per-account FR-7 + FR-8, delegated admins FR-9).
- **Verify in wafa-raw.json:** `account_info.account_type == "management"`; org/CT/CUR/StackSets
  checks are present.

### C2. Member account = LIMITED assessment
- **Precondition:** Run from a member (non-management) account in an org.
- **Expected:** Console prints a WARNING about member account; only identity (no-IAM-users) +
  per-account FR-7.1–7.6 checks run (~9 checks). Org/CT/CUR/StackSets checks absent.
- **Verify:** `account_type == "member"`; maturity level pinned at 1.

### C3. Standalone account = LIMITED assessment
- **Precondition:** Account not in any AWS Organization.
- **Expected:** WARNING that account is not in an org; same limited check set as C2;
  `account_type == "standalone"`; maturity level 1.

### C4. Org exists / management-account-identified always run
- **Expected:** In all three modes, "AWS Organization exists" and "Management account
  identified" checks appear.

---

## Group D — Region & partition handling

Owner: ____

### D1. Region discovery returns enabled regions only
- **Expected:** Opt-in regions (e.g. ap-east-1, me-south-1) are NOT in the scanned set unless enabled;
  count printed matches the `describe-regions` list filtered to `opt-in-not-required` and `opted-in`,
  and the printed opted-in count matches the enabled opt-in regions.

### D2. Region discovery API failure → fallback list
- **How to simulate:** Deny `ec2:DescribeRegions` on the role (or force an error).
- **Expected:** Warning printed; falls back to the hardcoded 17-region `DEFAULT_REGIONS`; assessment still completes.

### D3. Resource found only in a non-us-east-1 region is detected
- **Precondition:** Create a detectable resource (e.g. a CloudTrail trail or a VPC) ONLY in eu-west-1.
- **Expected:** The relevant check reflects it (e.g. "CloudTrail trail exists" = complete;
  "No VPCs in account" = incomplete). Confirms the multi-region fan-out actually works and isn't us-east-1-only.

### D4. Opt-in region enabled in the account
- **Precondition:** Enable an opt-in region; place a resource there.
- **Expected:** WAFA scans the opted-in region and the relevant check reflects the resource. If the
  Control Tower landing zone or IAM Identity Center instance is homed in that region, the Control
  Tower and Identity Center checks report complete.

### D5. GovCloud partition (if available)
- **Expected:** Partition detected as `aws-us-gov`; default region us-gov-west-1; CUR "Data Exports"
  (bcm-data-exports) path skipped; assessment completes.

### D6. China partition (if available)
- **Expected:** Partition `aws-cn`; default region cn-north-1; Data Exports path skipped.

For D5 and D6, also run locally with the Region set only in the profile (unset
`AWS_DEFAULT_REGION` and `AWS_REGION`). **Expected:** the partition is still
detected correctly, and "No IAM users in account" does not report `error`.

### D7. Region denied by an SCP
- **Precondition:** An SCP (for example, the Control Tower Region deny control) denies the
  assessment role in at least one enabled region; a CloudTrail trail exists in an allowed region.
- **Expected:** "CloudTrail trail exists" = complete. In an account with no trail, the check reports
  `error` and its message names the denied regions rather than reporting incomplete.

---

## Group E — Individual checks (spot-verification)

Owner: ____

For each, set up the "good" and "bad" state and confirm status flips correctly.

### E1. CloudTrail org trail (FR-7.2)
- Logging org trail present → complete; absent → incomplete.
- Stop logging on the only trail (`aws cloudtrail stop-logging`) → "CloudTrail trail exists" and
  "CloudTrail organization trail" = incomplete; restart logging → complete.

### E2. AWS Config recorder active (FR-7.3)
- Recorder recording in ≥1 region → complete; none recording → incomplete.

### E3. Config delivery channel (FR-7.4)
- A channel with `configHistoryDeliveryInfo.lastStatus == SUCCESS` or
  `configSnapshotDeliveryInfo.lastStatus == SUCCESS` → complete. If history
  delivery failed but snapshot delivery succeeded, the check is complete.

### E4. No EC2 instances / No VPCs (FR-7.5 / 7.6, "absence" checks)
- Zero instances/VPCs across all regions → complete; any present → incomplete.
- **Edge:** confirm the default VPC counts as "present" (it usually does).
- **Edge:** an instance terminated within the last hour does not make "No EC2 instances" incomplete.

### E4a. Account and OU names (FR-3.3–3.5, FR-3.10–3.12)
- Accounts named `LogArchive` / `Security_Tooling` and OUs named `SECURITY` or `work-loads` → the
  matching checks are complete. A suspended or closed "Log Archive" account → incomplete and it is
  not counted toward "Minimum 4 accounts".

### E5. Cost and Usage Report (FR-7.7, management only)
- Legacy CUR present → complete; none but a CUR 2.0 Data Export present → complete;
  neither → incomplete; API error with no report → error (not false incomplete).
- Paginated results: a report on a later page is found via `NextToken`, and
  empty pages end in incomplete rather than looping.

### E6. Org service integrations (FR-4)
- Enable/disable a service (e.g. GuardDuty) at the org level and confirm the matching
  check flips. Confirm all 10 come from a single API call (one failure marks all 10 error).

### E7. Policy types (SCP / Tag / Backup / RCP, FR-3.6–3.9)
- Enable a policy type on the root and confirm the check flips to complete.

### E8. OUs (Security / Workloads / Infrastructure, FR-3.10–3.12)
- Create an OU named "Security" → check complete (case-insensitive match).

### E9. Control Tower (FR-5)

- With a landing zone: deployed = complete; introduce drift → "not drifted" =
  incomplete; behind latest version → "latest version" = incomplete. No landing
  zone after a successful search → checks incomplete (not error). A missing or
  unrecognized drift status → "not drifted" = error, never complete.
- In mocked tests, a failed default-region `ListLandingZones` call still
  searches enabled opt-in regions. A landing zone found there can complete all
  three checks; if none is found, the checks report `error` instead of treating
  the failure as confirmed absence.

### E10. IAM Identity Center (FR-6.1) permission vs. absence
- SSO configured → complete; not configured anywhere → incomplete;
  `sso:ListInstances` denied or throttled in a region with no instance found elsewhere →
  **error** (not incomplete) — confirm the distinction.

### E11. No IAM users (FR-6.2)
- Zero IAM users → complete; ≥1 user → incomplete.

### E12. Organization and OU account map (FR-12, informational)
- Management account. **Expected:** the report's Organization Map section lists every OU
  and shows each account under the OU that actually contains it. Compare against the
  AWS Organizations console — the hierarchy and placement should match exactly.
- Confirm the map changes **no** check result and **no** maturity level: run before and
  after, and diff `wafa-checks.csv`. It must be identical.
- Suspended accounts appear with their status rather than being omitted.
- Account email addresses appear **nowhere** in any artifact (grep the outputs for `@`).
- **Directly attached vs inherited:** attach an SCP to a parent OU only. **Expected:** it
  is listed on the parent, and *not* repeated on child OUs or accounts. This is the most
  commonly misread part of the output.
- Member/standalone account. **Expected:** no Organization Map section (absent, not an
  errored section), and `wafa-org-map.csv` holds only the header and a `Not available` row
  saying the map runs only from the management account.
- **No stale map (FR-12.4):** run from the management account, then run into the same
  output directory or S3 prefix from a member account, or with Organizations reads denied.
  **Expected:** `wafa-org-map.csv` is replaced by a `Not available` row giving the reason;
  none of the first run's OUs, accounts, or policies remain.
- **Deeply nested hierarchy (6+ levels):** the OU where the walk stopped shows the warning
  on its own row, naming how many child OUs are not listed, and the section header warning
  names that OU's path. The same condition must not be stated twice on one surface.
- **Large hierarchy in the report:** the structure table pages rather than rendering every
  row, each row shows its OU path so a row on page 2 is still locatable, and the filter
  finds an account by name or ID. The table must not be sortable — the row order is the
  hierarchy.
- **A failed listing is visible without finding the row (FR-12.6):** deny
  `ListAccountsForParent` for one OU, then check the report shows an Alert above the table
  saying the counts are lower bounds and naming that OU, the CSV root row says the same,
  and the console prints the count. Filter the table so the affected row is hidden and
  confirm the Alert is still there — a row note alone is not sufficient.

### E13. Policy type coverage in the map (FR-12.3)
- With RCPs not enabled, confirm the section states "RCP is not enabled" rather
  than silently showing nothing (and "SCP and RCP are not enabled" when both are off).
- Immediately after enabling or disabling a policy type, while the root reports it as
  `PENDING_ENABLE` or `PENDING_DISABLE`: **expected:** the type appears in
  `policyTypesPending`, not in `policyTypesNotEnabled`, and the report, the CSV root row,
  and the console all say it is changing state and its attachments are not mapped.
- A policy lookup failure is labelled like the policy badges (for example
  `Tag: RequireCostCenterTag`), never with the API constant, in the report, the CSV, and
  the console.
- Enable Tag or Backup policies and attach one. **Expected:** it appears under the
  "Other Directly Attached Policies" CSV column and as a badge in the report.
- Attach an SCP directly to an *account*. **Expected:** it appears on that account's row,
  with no flag or extra option needed.
- Large organization (hundreds of OUs): **expected:** completes without throttling, and
  `policyLookupErrors` in `wafa-raw.json` is empty. Record timings and the call count.
  Attachment lookups should not grow with the number of OUs, only with the number of
  policies. The OU walk still costs two calls per OU, so it is the dominant call count on
  a large organization; it runs a level at a time through the bounded pool, so compare the
  wall-clock time against that call count rather than expecting it to be flat.
- Throttle deliberately (many policies, or run concurrently with other Organizations
  callers). **Expected:** the report warns "Attached policies may be incomplete" and names
  the affected policy type or policy; it never shows an empty policy list as if nothing
  were attached.
- **Two AWS answers disagreeing:** if `ListPolicies` returns `PolicyTypeNotEnabledException`
  or `UnsupportedAPIEndpointException` for a type the root reports as `ENABLED`, expect that
  type in `policyLookupErrors` saying so. It must not be reported as an absence: the map
  would then show the type as enabled with no attachment anywhere, which looks exactly like
  a type that is enabled but unused. (Hard to force deliberately; check the handling in
  `src/checks/org_map.py` and its unit test.)
- **Console warning volume:** with many node or policy warnings, confirm the console prints
  at most ten and states how many were withheld, and that the check results table is still
  visible above the prompt. Every warning must still be in the HTML report and the CSV.

---

## Group F — Maturity scoring

Owner: ____

### F1. Level 1 (baseline)
- Member/standalone or org missing core checks → level 1; `next_level_progress` and
  `next_level_checks_needed` populated.

### F2. Level advancement gating
- Satisfy all Level 2 checks + any one OU → level 2. Confirm you cannot reach level 3
  until ALL level-3 checks complete (gated ladder, not a percentage).

### F3. next_level_checks_needed ordering
- Confirm the "what to fix next" list is sorted by weight descending (highest impact first).

### F4. Level 5 requires everything
- Confirm level 5 only when StackSets org access enabled AND every check is complete.

---

## Group G — Report output & rendering

Owner: ____

### G1. Capability coverage chart renders (regression guard)
- Open wafa-report.html in a browser. **Expected:** the Cloudscape capability
  coverage bar chart displays all 7 axes, with Networking & Connectivity at 0%
  and a note explaining the gap.
- **Why:** guards the inlined Cloudscape bundle and the axis payload.

### G2. Report opens offline
- Disconnect network, open the HTML. **Expected:** fully renders (no CDN
  dependency; the Cloudscape JS, CSS, fonts, and report data are inlined).
- With JavaScript disabled, the `<noscript>` message points to the CSV and JSON.

### G3. Light/dark theme toggle
- The sun/moon icon at the top right of the top navigation switches Cloudscape
  light and dark modes; both themes are readable. The initial mode follows the
  browser's color-scheme preference.

### G3a. Repository footer
- The report footer names `aws-samples/sample-wa-foundations-assessment` and
  links to <https://github.com/aws-samples/sample-wa-foundations-assessment>.

### G4. Check table filters
- The status segmented control (All/Complete/Incomplete/Errors) and the text
  filter narrow table rows correctly.

### G5. CSV export integrity
- Open wafa-checks.csv. **Expected:** header row + one row per check; 8 columns; error
  column populated only for error checks.

### G6. Raw JSON schema
- wafa-raw.json contains `account_info`, `maturity`, `checks[]`, `delegated_administrators[]`,
  and `organization_map` (management account) or `null` (limited mode).
  The HTML report data uses the same `delegated_administrators`,
  `delegated_administrators_error`, `organization_map`, and `organization_map_error` keys.
  Every check has: check, description, status, required, weight, loe, remediationLink (+ error if error).

### G7. Special characters don't break the report
- If any account/OU name contains `<`, `>`, `&`, or quotes, confirm the HTML escapes it
  (no broken layout / no injected markup). Validates the sandboxed autoescaping.

### G8. Delegated admins table (management only)
- With ≥1 delegated administrator, confirm the table lists account + services;
  with none, confirm the section handles empty gracefully.

### G9. Organization map CSV (FR-12.4)
- `wafa-org-map.csv` opens in a spreadsheet with 10 columns and one row per root,
  OU, and account.
- Confirm it agrees with the report's Organization Map table. The report shows one
  badge column for all attached policies, while the CSV splits them into SCP, RCP,
  and other columns — check the same policies appear, not the same layout.
- An OU or account name containing a comma or quote round-trips correctly
  (the csv module quotes it).
- **Formula injection:** if any OU or account name begins with `=`, `+`, `-` or `@`,
  confirm the cell is prefixed with an apostrophe and the spreadsheet shows it as
  literal text rather than evaluating it.
- **Partial maps are flagged (FR-12.6):** the root row's Error cell must say the map is
  incomplete in every case — a denied or throttled policy lookup (full message, since it
  belongs to no row), a failed account or child-OU listing, or a walk stopped at the
  nesting limit (a count plus the affected paths). The affected row's own Error cell
  carries the explanation. Check both: an incomplete map must never look complete to
  someone reading only the first row, and the same sentence must not appear twice.
- **Formula escaping is narrow and complete:** a name of `" =HYPERLINK(...)"` with a
  leading space is still escaped, and a plain signed number such as `-1` is left alone so
  it still matches `wafa-raw.json`.

---

## Group H — Resilience & error handling

Owner: ____

### H1. Single check failure doesn't abort the run
- Deny one API (e.g. `config:DescribeConfigurationRecorders`). **Expected:** that check =
  error with a message; all other checks still run; reports still generated.

### H2. Partial permissions
- Run with a role missing several assessment permissions. **Expected:** graceful degradation —
  affected checks = error, assessment completes, error count shown in summary.
- Deny `organizations:DescribeOrganization`. **Expected:** the run does not stop; account type is
  `unknown`, the limited checks run, "Management account identified" = error, and the report shows
  why the account type could not be determined.
- Deny `organizations:ListDelegatedAdministrators`. **Expected:** the Delegated Administrators section
  still appears with a warning; `wafa-raw.json` has `delegated_administrators_error`.
- If listing one delegated administrator's services fails, **expected:** that
  administrator retains any services already found and has `servicesError`;
  later administrators still appear without a list-level error.
- Deny `organizations:ListPolicies` or `organizations:ListTargetsForPolicy`. **Expected:**
  the Organization Map still shows the hierarchy and account placement, with a warning that
  attached policies may be incomplete; the assessment and maturity level are unaffected.
- Deny `organizations:ListAccountsForParent`. **Expected:** affected OUs show an account
  listing warning and sibling branches still list their accounts.

### H3. CodeBuild timeout boundary
- Very large org / many regions. **Expected:** completes within the 10-min CodeBuild timeout,
  or document how close it gets (perf signal for tuning max_workers).

### H4. Idempotent re-run
- Run twice back to back. **Expected:** second run overwrites reports; no residue/errors from first.

---

## Priority for a first testing pass

If testers are limited, cover these first (highest signal):
1. A1, A2, **A3** (the suspected remaining region bug)
2. C1, C2, C3 (account modes)
3. G1, G2 (report + capability chart regression guards)
4. D3 (multi-region resource detection)
5. H1 (resilience)
