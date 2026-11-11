# WAFA Assessment — Requirements Specification

## Overview

Build a Well-Architected Foundations Assessment (WAFA) tool that evaluates an AWS Organization's multi-account architecture against best practices. The tool runs as a CodeBuild project deployed via CloudFormation, produces an HTML report stored in S3, and requires only read-only assessment access to the customer's management account.

WAFA is inspired by the Cloud Foundation Assessment pattern, but its implementation, report names, and deployment flow are defined by this repository.

## Functional Requirements

### FR-1: Deployment

- FR-1.1: Deploy via a single CloudFormation template in the customer's AWS account (management account recommended)
- FR-1.2: CloudFormation creates these resources:
  - IAM Role with explicit read-only permissions (see "AWS API Permissions Required" section)
  - CodeBuild project (Python 3.12 runtime, `aws/codebuild/standard:7.0` image)
  - S3 bucket (encrypted AES256, public access blocked, versioning enabled)
  - Optional: SNS topic (created only if `EmailAddress` parameter is provided)
  - Lambda-backed Custom Resource that starts the first CodeBuild run
  - The deployment script creates and uploads the source S3 bucket/object before deploying the stack; the stack does not clone a Git repository.
- FR-1.3: CodeBuild triggers automatically on stack creation via a Custom Resource Lambda:
  - Lambda defined inline in CloudFormation (ZipFile property), runtime `python3.12`
  - Handler calls `codebuild.start_build(projectName=...)` on the project created in the same stack
  - Lambda IAM role has only `codebuild:StartBuild` permission scoped to the project ARN
  - For CloudFormation Delete requests, the Lambda is a no-op and returns SUCCESS without taking action
  - The `AutoStartBuild` parameter defaults to `true` for direct CloudFormation
    deployments. `deploy.sh` sets it to `false`, starts one build directly, and
    waits on the exact build ID returned by CodeBuild.
- FR-1.4: Support manual re-run: customer clicks "Start build" in CodeBuild console
- FR-1.5: Auto-detect partition from the configured AWS region (`AWS_DEFAULT_REGION`, then `AWS_REGION`, then the active profile's region):
  - `us-gov-*` regions → `aws-us-gov` partition, default region `us-gov-west-1`
  - `cn-*` regions → `aws-cn` partition, default region `cn-north-1`
  - All other → `aws` partition, default region `us-east-1`

### FR-2: Account Discovery

- FR-2.1: Call `sts.getCallerIdentity()` to get current account ID
- FR-2.2: Call `organizations.describeOrganization()`:
  - If succeeds: account is in an org. Compare `Organization.MasterAccountId` with current account ID
  - If `MasterAccountId == currentAccountId`: this IS the management account → run ALL checks
  - If `MasterAccountId != currentAccountId`: this is a MEMBER account → print warning, run limited checks
  - If throws `AWSOrganizationsNotInUseException`: standalone account → print warning, run limited checks
  - If throws any other error (for example, AccessDenied): account type `unknown` → print warning, record `detection_error`, run limited checks; FR-3.2 reports `error`
- FR-2.3: Discover regions by calling `ec2.describeRegions(Filters=[{Name: 'opt-in-status', Values: ['opt-in-not-required', 'opted-in']}])`
  - Returns default-enabled regions plus opt-in regions the account has enabled; opt-in regions that are not enabled are excluded
  - The opted-in subset is passed to FR-5 so a landing zone homed in an opt-in region is found
  - If this call fails, fall back to a partition-specific list: 17 commercial regions, 2 GovCloud regions, or 2 China regions
- FR-2.4: When NOT management account, run ONLY these checks:
  - FR-6.2 (IAM Users)
  - FR-7.1 (CloudTrail exists)
  - FR-7.2 (CloudTrail is org trail — member accounts can see organization trails, so this can pass in limited mode)
  - FR-7.3 (Config recorder)
  - FR-7.4 (Config delivery channel)
  - FR-7.5 (EC2 instances)
  - FR-7.6 (VPCs)
  - FR-3.1 and FR-3.2 also run in every mode. Management-account-only checks are skipped rather than synthesized as incomplete results.

### FR-3: Organization Governance Checks

**Prerequisite:** FR-3.1 and FR-3.2 run in every account mode. FR-3.3 through FR-3.12 run only when FR-2.2 determined this is the management account.

- FR-3.1: **Org Exists**
  - API: `organizations.describeOrganization()`
  - Pass: response contains `Organization.Id`
  - Status: "complete" if org exists, "incomplete" if not
  - weight: 6, loe: 1, required: true

- FR-3.2: **Management Account Identified**
  - Logic: `Organization.MasterAccountId == currentAccountId`
  - Pass: they match
  - Status: "complete" if match, "incomplete" if not
  - weight: 6, loe: 1, required: true

- FR-3.3: **Minimum 4 Accounts**
  - API: `organizations.listAccounts()` (paginate fully); count only accounts whose `State` (or legacy `Status`) is `ACTIVE`
  - Pass: active account count >= 4
  - Status: "complete" if >= 4, "incomplete" if < 4
  - weight: 6, loe: 2, required: true

- FR-3.4: **Log Archive Account Exists**
  - Logic: from FR-3.3 account list, check if any active account has a normalized `Name` == "logarchive" (case, spaces, hyphens, and underscores ignored)
  - Status: "complete" if found, "incomplete" if not
  - weight: 6, loe: 2, required: true

- FR-3.5: **Audit Account Exists**
  - Logic: from FR-3.3 account list, check if any active account has a normalized `Name` == "audit" OR "securitytooling" (case, spaces, hyphens, and underscores ignored)
  - Status: "complete" if found, "incomplete" if not
  - weight: 6, loe: 2, required: true

- FR-3.6: **SCP Enabled**
  - API: `organizations.listRoots()`
  - Logic: check `Roots[0].PolicyTypes` for entry with `Type == "SERVICE_CONTROL_POLICY"` AND `Status == "ENABLED"`
  - Status: "complete" if enabled, "incomplete" if not
  - weight: 6, loe: 1, required: true

- FR-3.7: **Tag Policy Enabled**
  - Same API as FR-3.6
  - Logic: check for `Type == "TAG_POLICY"` AND `Status == "ENABLED"`
  - Status: "complete" if enabled, "incomplete" if not
  - weight: 6, loe: 1, required: true

- FR-3.8: **Backup Policy Enabled**
  - Same API as FR-3.6
  - Logic: check for `Type == "BACKUP_POLICY"` AND `Status == "ENABLED"`
  - Status: "complete" if enabled, "incomplete" if not
  - weight: 5, loe: 1, required: false

- FR-3.9: **RCP Enabled**
  - Same API as FR-3.6
  - Logic: check for `Type == "RESOURCE_CONTROL_POLICY"` AND `Status == "ENABLED"`
  - Status: "complete" if enabled, "incomplete" if not
  - weight: 4, loe: 1, required: false

- FR-3.10: **Security OU Exists**
  - API: `organizations.listOrganizationalUnitsForParent(ParentId=rootOuId)`
  - Logic: check if any OU has normalized `Name` == "security" (case, spaces, hyphens, and underscores ignored)
  - Prerequisite: `rootOuId` comes from `listRoots()[0].Id`
  - Status: "complete" if found, "incomplete" if not
  - weight: 6, loe: 2, required: true

- FR-3.11: **Workloads OU Exists**
  - Same API as FR-3.10
  - Logic: check for normalized `Name` == "workloads" (case, spaces, hyphens, and underscores ignored)
  - Status: "complete" if found, "incomplete" if not
  - weight: 5, loe: 2, required: false

- FR-3.12: **Infrastructure OU Exists**
  - Same API as FR-3.10
  - Logic: check for normalized `Name` == "infrastructure" (case, spaces, hyphens, and underscores ignored)
  - Status: "complete" if found, "incomplete" if not
  - weight: 5, loe: 2, required: false

### FR-4: Organization Service Integration Checks

**Prerequisite:** Only run if management account.

- API: Single call to `organizations.listAWSServiceAccessForOrganization()` (paginate fully)
- Returns list of `EnabledServicePrincipals[].ServicePrincipal`
- Each sub-check below looks for a specific service principal in that list:

| ID | Check Name | Service Principal to find | weight | loe | required |
|----|-----------|--------------------------|--------|-----|----------|
| FR-4.1 | CloudTrail Org Service | `cloudtrail.amazonaws.com` | 6 | 1 | true |
| FR-4.2 | Config Org Service | `config.amazonaws.com` | 4 | 1 | false |
| FR-4.3 | GuardDuty Org Service | `guardduty.amazonaws.com` | 4 | 1 | false |
| FR-4.4 | Security Hub Org Service | `securityhub.amazonaws.com` | 4 | 1 | false |
| FR-4.5 | IAM Access Analyzer Org Service | `access-analyzer.amazonaws.com` | 4 | 1 | false |
| FR-4.6 | RAM Org Service | `ram.amazonaws.com` | 4 | 1 | false |
| FR-4.7 | IAM Identity Center Org Service | `sso.amazonaws.com` | 6 | 1 | true |
| FR-4.8 | CloudFormation Org Service | `member.org.stacksets.cloudformation.amazonaws.com` | 5 | 1 | false |
| FR-4.9 | Backup Org Service | `backup.amazonaws.com` | 4 | 1 | false |
| FR-4.10 | Cost Optimization Hub Org Service | `cost-optimization-hub.bcm.amazonaws.com` | 4 | 1 | false |

- Status for each: "complete" if service principal found in list, "incomplete" if not

### FR-5: Control Tower Checks

**Prerequisite:** Only run if management account.

- FR-5.1: **Control Tower Deployed**
  - API: `controltower.listLandingZones()`. If the default-region call is
    empty or fails, search each opted-in region from FR-2.3. A landing zone
    homed in an opt-in region is listed only from that region. Opted-in
    regions that botocore's endpoint data lists without a Control Tower
    endpoint are skipped. Other regional failures, including a connection
    failure in the default region, do not stop the search.
  - Pass: response `landingZones` array is non-empty
  - Status: "complete" if found, "incomplete" if no landing zone is found and
    no non-skipped call failed, "error" if no landing zone is found and a
    non-skipped call failed
  - weight: 6, loe: 6, required: true

- FR-5.2: **Control Tower Not Drifted**
  - Logic: from FR-5.1, get first landing zone ARN, then `controltower.getLandingZone(landingZoneIdentifier=arn)`
  - Pass: `landingZone.driftStatus.status == "IN_SYNC"`
  - Status: "complete" if `IN_SYNC`, "incomplete" if `DRIFTED`, "error" if the drift status is missing or any other value
  - weight: 6, loe: 2, required: true

- FR-5.3: **Control Tower Latest Version**
  - Logic: from same `getLandingZone` response
  - Pass: `landingZone.version == landingZone.latestAvailableVersion` and both are non-empty
  - Status: "complete" if versions match and are non-empty, "incomplete" otherwise (missing version fields are not a pass)
  - weight: 5, loe: 2, required: false

### FR-6: Identity Checks

- FR-6.1: **IAM Identity Center Configured** (management account only)
  - API: `sso-admin.listInstances()` — iterate regions from FR-2.3 SEQUENTIALLY, stop on first success
  - Pass: any region returns non-empty `Instances` array
  - Status: "complete" if found in any region; otherwise "error" if any region failed (permissions, throttling, or service errors), else "incomplete". Regions that botocore's endpoint data lists without an Identity Center endpoint are skipped; a connection failure in any other region is an error.
  - weight: 6, loe: 3, required: true

- FR-6.2: **No IAM Users in Assessed Account** (runs in ALL modes)
  - API: `iam.listUsers()` in the partition default region from FR-1.5 (IAM is global within a partition)
  - Pass: `Users` array is empty (length == 0)
  - Status: "complete" if NO users, "incomplete" if users exist
  - weight: 4, loe: 1, required: false

### FR-7: Per-Account Resource Checks

**These run in ALL modes** (management, member, standalone).

- FR-7.1: **CloudTrail Trail Exists**
  - API: `cloudtrail.describeTrails()` for EACH region from FR-2.3, then `cloudtrail.getTrailStatus(Name=TrailARN)` for the returned trails
  - Pass: at least one trail has `IsLogging == true`
  - Status: "complete" if any logging trail found, "incomplete" if none
  - weight: 6, loe: 3, required: true

- FR-7.2: **CloudTrail Org Trail**
  - Logic: from FR-7.1 results, check if any trail has `IsOrganizationTrail == true` and `IsLogging == true`
  - Pass: at least one logging org trail exists (member accounts can see organization trails)
  - Status: "complete" if found, "incomplete" if not
  - weight: 6, loe: 1, required: true

- FR-7.3: **Config Recorder Active**
  - API: `config.describeConfigurationRecorders()` for EACH region
  - API: `config.describeConfigurationRecorderStatus()` for each region with a recorder
  - Pass: at least one configured recorder has `recording == true`
  - Status: "complete" if any recorder is actively recording, "incomplete" if none
  - weight: 6, loe: 2, required: true

- FR-7.4: **Config Delivery Channel Active**
  - API: `config.describeDeliveryChannels()` for EACH region
  - API: `config.describeDeliveryChannelStatus()` for each region with a channel
  - Pass: at least one channel reports successful configuration-history or snapshot delivery
  - Status: "complete" if any channel reports successful delivery, "incomplete" if none
  - weight: 6, loe: 2, required: true

- FR-7.5: **No EC2 in Management Account**
  - API: `ec2.describeInstances(Filters=[{Name: 'instance-state-name', Values: ['pending', 'running', 'stopping', 'stopped']}])` for EACH region
  - Pass: NO region has any non-terminated instance (terminated instances stay visible for about an hour and are ignored)
  - Status: "complete" if NO instances anywhere, "incomplete" if any found
  - weight: 4, loe: 1, required: false

- FR-7.6: **No VPCs in Management Account**
  - API: `ec2.describeVpcs()` for EACH region
  - Pass: NO region returns any VPCs (note: original CFAT flags ANY VPC including defaults)
  - Status: "complete" if NO VPCs anywhere, "incomplete" if any found
  - weight: 4, loe: 1, required: false

- FR-7.7: **Legacy CUR Setup** (management account only)
  - APIs: `cur.describeReportDefinitions()` and, in the commercial partition, `bcm-data-exports.list_exports()`, using the partition default region (us-east-1 for aws, us-gov-west-1 for aws-us-gov, cn-north-1 for aws-cn)
  - Pagination: follow `NextToken` for both APIs
  - Pass: a legacy report definition or Data Exports export exists
  - Status: "complete" if any report/export is found, "incomplete" if neither API returns a report/export; return "error" if API failures prevent determining that no report exists
  - weight: 4, loe: 1, required: false

### FR-8: CloudFormation StackSets Check

**Prerequisite:** Only run if management account.

- FR-8.1: **StackSets Org Access Enabled**
  - API: `cloudformation.describeOrganizationsAccess()`
  - Pass: response `Status == "ENABLED"`
  - Status: "complete" if enabled, "incomplete" if disabled, "error" if the API call fails (per NFR-3.2)
  - weight: 5, loe: 1, required: false

### FR-9: Delegated Administrator Checks

**Prerequisite:** Only run if management account. These are INFORMATIONAL only (no pass/fail check generated).

- FR-9.1: Call `organizations.listDelegatedAdministrators()` (paginate fully)
- FR-9.2: For each delegated admin account, call `organizations.listDelegatedServicesForAccount(AccountId=...)`
- FR-9.3: Display as a separate table in the HTML report (below the checks table), titled "Delegated Administrators"
  - Columns: Account ID, Account Name, Delegated Services (comma-separated)
  - Also included in `wafa-raw.json`
  - NOT scored — informational reference only
- Output: list of `{accountId, accountName, services: [servicePrincipal, ...], servicesError?}`
  plus `delegated_administrators_error` in `wafa-raw.json` when the
  administrator list could not be fully retrieved. A service-list failure keeps
  that administrator and any services already found, records `servicesError`,
  and does not prevent later administrators from being listed. Administrators
  collected before a list-level failure are kept, and the report shows the
  error instead of implying none exist. The HTML report data uses the same
  delegated-administrator keys as `wafa-raw.json`.

### FR-10: Report Generation

- FR-10.1: Generate an interactive HTML report containing:
  - **Maturity Level Score** (top of report): Level 1-5 with name, progress bar to next level, and specific next steps
  - **Capability Coverage Chart**: Cloudscape horizontal bar chart showing % completion per WA Foundations capability (7 axes):
    - Axis 1: Multi-Account Environment (FND01) — checks FR-3.1-3.12
    - Axis 2: Identity & Access Management (FND02) — checks FR-3.6, FR-3.9, FR-4.5, FR-4.7, FR-6.1, FR-6.2
    - Axis 3: Central Observability (FND03) — checks FR-4.1, FR-7.1, FR-7.2 (limited in Phase 1)
    - Axis 4: Resource & Inventory Management (FND04) — checks FR-3.7, FR-3.8, FR-4.2, FR-4.9
    - Axis 5: Security & Compliance (FND05) — checks FR-4.1, FR-4.3, FR-4.4, FR-7.1-7.4, FR-3.4
    - Axis 6: Cloud Financial Management (FND06) — checks FR-3.7, FR-4.10, FR-7.7
    - Axis 7: Networking & Connectivity (FND07) — no Phase 1 checks (shows 0%, gap highlighted)
    - Score per axis = (passing checks in that capability / total checks for that capability) × 100
    - Note: A single check CAN contribute to multiple axes (e.g., CloudTrail is both Observability and Security) — axes are independent dimensions, not a partition
    - Implementation: Cloudscape Design System components (React), built from `report-ui/` into `src/report/assets/` and inlined in the HTML with the assessment data as JSON; the report has no external CDN or network dependency
    - Reference pattern: https://github.com/aws-samples/sample-aws-observability-assessment
  - Executive summary: total checks, passed, failed, completion percentage
  - Table with columns: Check Name, Description, Status, Required (yes/no), Level of Effort, Remediation Link
  - Filter controls: show all / complete only / incomplete only / error only, plus text filtering
  - Console table print (same as original CFAT): columns `check`, `status`, `required`, `loe`
  - Light/dark mode toggle in the top navigation (Cloudscape `applyMode`)
- FR-10.2: Generate CSV file with ALL check results (same columns as HTML table) — filename: `wafa-checks.csv`
- FR-10.3: Generate raw JSON file with full assessment data — filename: `wafa-raw.json`
- FR-10.4: Upload all files to S3 bucket: `s3://{bucket}/{account_id}/wafa-report.html`, `wafa-checks.csv`, `wafa-raw.json`
- FR-10.5: If SNS topic exists and `EmailAddress` parameter was provided, publish a notification from `post_build`: a completion message with the S3 URL when `CODEBUILD_BUILD_SUCCEEDING` is `1`, otherwise a failure message with the build ID
- FR-10.6: `python -m src.main` exits non-zero when every check returns `error` (usually missing permissions); partial errors still exit 0

### FR-11: Maturity Level Scoring

Based on the AWS Well-Architected Foundations maturity model (March 2026). Maps check results to a maturity level and shows customers their current position + what to do next.

- FR-11.1: Calculate maturity level based on these criteria:

  **Level 1 — Base** (any of these true):
  - FR-3.1 incomplete (no org) OR
  - Only 1-2 accounts OR
  - No OU structure at all

  **Level 2 — Established** (all required):
  - FR-3.1 complete (org exists)
  - FR-3.2 complete (management account identified)
  - FR-3.3 complete (4+ accounts)
  - At least 1 OU exists (any of FR-3.10, FR-3.11, FR-3.12)

  **Level 3 — Intermediate** (Level 2 + all required):
  - FR-3.6 complete (SCPs enabled)
  - FR-3.10 complete (Security OU)
  - FR-5.1 complete (Control Tower deployed)
  - FR-3.7 complete (Tag Policy enabled)
  - FR-4.1 complete (CloudTrail org service)
  - FR-6.1 complete (IAM Identity Center configured)
  - FR-7.1 complete (CloudTrail exists)
  - FR-7.3 complete (Config recorder active)

  **Level 4 — Advanced** (Level 3 + all required):
  - FR-4.3 complete (GuardDuty org service)
  - FR-4.4 complete (Security Hub org service)
  - FR-4.5 complete (Access Analyzer org service)
  - FR-3.8 complete (Backup Policy enabled)
  - FR-3.9 complete (RCP enabled)
  - FR-6.2 complete (no IAM users in mgmt)
  - FR-7.5 complete (no EC2 in mgmt)
  - FR-7.6 complete (no VPCs in mgmt)
  - FR-5.2 complete (Control Tower not drifted)
  - FR-5.3 complete (Control Tower latest version)
  - ALL FR-4.x complete (all org services enabled)

  **Level 5 — Expert** (Level 4 + all required):
  - FR-8.1 complete (StackSets org access)
  - ALL checks returned by the current Phase 1 implementation are complete; both "incomplete" and "error" results prevent Level 5
  - Note: Full Level 5 validation requires Phase 2+ checks (root user, MFA, credential rotation) beyond Phase 1 scope

- FR-11.2: Report output includes:
  - Current level (1-5) with level name
  - Percentage progress toward next level (passing checks for next level / total needed for next level)
  - Ordered list of checks needed to reach next level (sorted by weight descending — highest impact first)

- FR-11.3: Maturity model definitions (shown in report):

  | Level | Name | Description |
  |-------|------|-------------|
  | 1 | Base | One or more prerequisites for an established multi-account foundation are not yet complete. |
  | 2 | Established | AWS Organizations, the management account, a minimum account baseline, and an organizational unit structure are established. |
  | 3 | Intermediate | Core governance, account provisioning, identity, audit logging, and resource recording foundations are in place. |
  | 4 | Advanced | Organization service integrations, preventive and detective controls, and management-account hygiene checks are complete. |
  | 5 | Expert | Every check returned by the current Well-Architected Foundations Assessment report is complete. |

### FR-12: Organization and OU Account Map
**Prerequisite:** Only run if management account. These are INFORMATIONAL only (no pass/fail check generated),
so FR-12 never contributes to FR-11 maturity scoring or to the capability axes. The OU and policy-type
*checks* in FR-3.6–FR-3.12 remain the scored view of the same services.
- FR-12.1: Walk the OU hierarchy from `organizations.listRoots()`, using
  `organizations.listOrganizationalUnitsForParent()` (paginate fully). Bound the walk at
  5 levels below the root, which is the AWS Organizations nesting limit.
  - Walk one level at a time, submitting both calls for every node on that level to the same bounded
    `ThreadPoolExecutor` used by FR-12.3 before reading any result, and consume the results in level order
    so the tree, the totals, and the per-node warnings stay deterministic. The call count is unchanged at
    two per OU; only the latency improves, and the pool stays small because Organizations throttles hard.
  - Record `childrenTruncated` on the node where the walk stopped, naming how many child OUs were not
    listed, so the gap is locatable. Flag truncation only when children actually exist past the limit: an
    OU sitting exactly at the limit with no children is not truncation.
  - Summarize the same condition at map level as `depthTruncated` with `depthTruncatedPaths` naming those
    nodes (see FR-12.6).
  - Build each node's `path` by joining names with `/`, escaping `\` and then `/` inside a name. AWS
    permits `/` in an OU name, so an OU literally named `Prod/EU` would otherwise produce the same path as
    an OU `EU` nested inside an OU `Prod`. Escaping the backslash first keeps the escaping reversible.
- FR-12.2: For each root and OU, call `organizations.listAccountsForParent()` (paginate fully) to record
  account placement. Include suspended accounts with their `status`; do not drop them. Record the account
  `id`, `name`, and `status` only — never the account email address, which is personal data not needed to
  place an account.
  - A failed account or child-OU listing is recorded on that node as `accountsError` or `childrenError`,
    and at map level as `nodeListingErrors` with `nodeListingErrorPaths` naming those nodes (see FR-12.6).
    The totals count what was read, so without the map-level field a partial map reads as a complete,
    smaller organization.
- FR-12.3: Collect policy attachments by enumerating policies, not by interrogating targets. For each policy
  type that is `ENABLED` on the root, call `organizations.listPolicies()` (paginate fully); then for each
  policy returned, call `organizations.listTargetsForPolicy()` (paginate fully) and attach it to the matching
  root, OU, or account, recording policy `id`, `name`, `type`, and `awsManaged`.
  - Both this and the per-target alternative report only **directly attached** policies, never inherited
    ones. An empty list means nothing is attached at that level, not that no policy applies.
  - Treat `enabled policy types + policies` as a **floor on the attachment lookups, not a total**.
    `listTargetsForPolicy` pages, so a policy attached to every target, as `FullAWSAccess` usually is,
    pages across all of them and its own lookup still scales with the organization. The FR-12.1 walk is
    also unchanged at two calls per OU, so on a large organization most of the calls are the walk rather
    than the policy lookups. What the inversion buys is that the attachment lookups stop growing with the
    number of OUs: interrogating each target instead costs `targets x enabled policy types`, which grows
    with the size of the organization and throttles on large ones. Account-level attachments arrive in the
    same responses, so they are always included and need no opt-in flag.
  - Only types enabled on the root are queried, so `PolicyTypeNotEnabledException` is expected not to
    occur. If `listPolicies` raises it or `UnsupportedAPIEndpointException` anyway, that is a disagreement
    between two AWS answers, not a confirmed absence: record it in `policyLookupErrors` naming the
    contradiction. Skipping it would show the type as enabled with no attachment anywhere, which is
    indistinguishable from "enabled but genuinely unused".
  - Use a bounded `ThreadPoolExecutor`, creating the client in the calling thread (see NFR-2.2), and await
    every lookup so one failure cannot hide another policy's attachments.
  - Record every lookup failure through one handler that selects the message by error code, so no later
    branch can swallow a failure instead of reporting it.
  - Configure the Organizations client with **adaptive** retries. Organizations enforces a low request rate
    and standard mode has no client-side rate limiting, so a burst of concurrent reads exhausts its attempts
    and silently loses attachments.
  - Any other failure is recorded in `policyLookupErrors` as `{scope, error}`, where `scope` is the policy
    type (when listing policies failed) or `"<type>: <name>"` (when listing one policy's targets failed).
    An error means attachments may be incomplete and must be surfaced rather than rendered as an empty list.
  - Report `policyTypesNotEnabled` for SERVICE_CONTROL_POLICY and RESOURCE_CONTROL_POLICY so the map states
    plainly when a guardrail type is not in use at all.
- FR-12.4: Generate `wafa-org-map.csv` with one row per root, OU, and account, separating directly attached
  SCPs, RCPs, and other policy types into their own columns. Keep it separate from `wafa-checks.csv`, which
  stays one row per check. Escape cells that begin with `=`, `+`, `-`, `@`, tab, or CR so AWS-provided
  names and error messages cannot execute as spreadsheet formulas.
  - The root row's Error cell carries every reason the map may be incomplete, so a reader who opens only
    the spreadsheet cannot mistake a partial map for a complete one: the `policyLookupErrors` messages,
    which belong to no row, plus a count and the affected paths for the conditions that do (see FR-12.6).
    It never displaces that row's own note.
  - Escape cells so AWS-provided text cannot execute as a spreadsheet formula: prefix with an apostrophe
    when the value begins with a tab, CR, or LF, or when it begins with `=`, `+`, `-`, or `@` after leading
    whitespace is ignored. A value that is a plain number is left alone, so a signed number still matches
    the same value in `wafa-raw.json`. This applies to `wafa-checks.csv` as well, whose `Error` column
    carries AWS API messages.
- FR-12.5: Display as a separate "Organization Map" section in the HTML report, below the check results,
  with an indented structure table, account placement, status, and attached-policy badges. Print a one-line
  console summary of OU, account, and attachment counts.
  - Page and filter the structure table, and show each row's OU path, because paging or filtering separates
    a row from the ancestors its indentation refers to. Do not sort it: the row order is the hierarchy.
  - Render display labels, not API constants, for policy types in every output including the console.
  - `wafa-raw.json` carries the complete map as `organization_map`, plus `organization_map_error` when it
    could not be built. The HTML report data carries `organization_map_rows` (rows flattened in Python with
    finished policy labels and a row key that leads with the row's position, because an AWS id defaults to
    an empty string and two rows missing an id would otherwise share a key) and
    `organization_map_summary`, which holds only the map-level fields the UI reads: `organizationId`,
    `totals`, `policyLookupErrors`, `depthTruncatedPaths`, and `nodeListingErrorPaths`. The HTML must not
    embed the tree as well, which would roughly double the inline JSON for data the UI never reads.
  - Flatten the tree once per run and pass the same rows to the console, the CSV, and the HTML report, so
    the three cannot disagree.
  - A per-node failure is recorded on that node as `accountsError` or `childrenError` and keeps the rest of
    the map, mirroring FR-9's partial-result behavior. Policy lookup failures are organization-wide rather
    than per-node and are reported through `policyLookupErrors`.
  - The console states how many nodes could not be fully listed before printing the individual warnings,
    then prints every map-level and per-node warning, each naming its node, capped with a count of the
    withheld lines. A throttled large organization can produce a warning per node, which would otherwise
    push the check results off-screen; the HTML report and the CSV carry them all.
  - NOT scored — informational reference only.
- FR-12.6: Every reason the map may be incomplete must be visible without reading the whole map. Each is
  recorded twice, with a different job in each place, and never as the same sentence twice:
  - On the node it affects (`accountsError`, `childrenError`, `childrenTruncated`), which explains what
    happened, and in the CSV and the report table is the row's own Error cell or note.
  - At map level, as a boolean plus the affected paths (`nodeListingErrors` / `nodeListingErrorPaths`,
    `depthTruncated` / `depthTruncatedPaths`), which states that the totals are lower bounds and locates
    the affected nodes. The report renders an Alert above the table, the CSV writes it to the root row, and
    the console prints the count. A per-row note alone is not sufficient: the row may be filtered out or on
    a later page of a paged table.
  - `policyLookupErrors` has no row of its own, so its message appears at map level only.
- FR-12.7: Reads that every organization check shares — `DescribeOrganization` and `ListRoots` — are
  resolved once per run through `src/checks/org_api.py` rather than once per check. Cache successful
  responses only, so one denied or throttled read does not become an error for every later check, and
  clear the cache at the start of a run. The pagination helpers live there too, so no scored check depends
  on an informational module for them.
- Output: `{organizationId, managementAccountId, root, enabledPolicyTypes, policyTypesNotEnabled,
  policyLookupErrors, totals: {organizationalUnits, accounts, maxDepth}, depthTruncated?,
  depthTruncatedPaths?, nodeListingErrors?, nodeListingErrorPaths?}`, where each node is
  `{id, name, type, path, depth, policies: [...], accounts: [...], children: [...]}` plus
  `accountsError?`, `childrenError?`, and `childrenTruncated?`, and each account is
  `{id, name, status, policies: [...]}`.

## Non-Functional Requirements

### NFR-1: Security
- NFR-1.1: ReadOnly access ONLY — tool makes ZERO changes to the AWS environment (no creates, updates, deletes)
- NFR-1.2: IAM role uses explicit action list (never `Action: "*"` or `Resource: "*"` except where required by API)
- NFR-1.3: S3 bucket: AES256 encryption, `BlockPublicAcls: true`, `BlockPublicPolicy: true`, `IgnorePublicAcls: true`, `RestrictPublicBuckets: true`, versioning enabled
- NFR-1.4: All data stays in the customer's AWS account — no external API calls or data exfiltration

### NFR-2: Performance
- NFR-2.1: Complete full assessment (all checks) within 2 minutes
- NFR-2.2: Use `concurrent.futures.ThreadPoolExecutor` for per-region checks (EC2, VPC, CloudTrail, Config run across the enabled regions, typically ~17 commercial regions)
  - Create clients in the calling thread; wait for every region. A match in any region decides the result even if other regions fail. With no match, any region that could not be inspected makes the check `error` (listing the regions), not `incomplete`.
- NFR-2.3: Single API call for org service checks (one `listAWSServiceAccessForOrganization` call serves all FR-4.x checks)

### NFR-3: Reliability
- NFR-3.1: Configure botocore standard retries for throttling and transient failures with `AWS_RETRY_MODE=standard` and `AWS_MAX_ATTEMPTS=5` unless the caller already set those variables
- NFR-3.2: On AccessDenied or another check-level API failure, mark that check as "error" with the exception message and continue to the next check; do not convert an unknown state to "incomplete"
- NFR-3.3: On a region-not-enabled response (`InvalidRegion`, `OptInRequired`, or equivalent message), skip that region and continue scanning
- NFR-3.4: Never crash the entire assessment due to a single check failure — always produce a report even if partial

### NFR-4: Extensibility
- NFR-4.1: Each check is a standalone function returning the Check Metadata Schema (below)
- NFR-4.2: All check metadata (name, description, weight, loe, required, remediationLink) is defined in the function — not in a separate config file
- NFR-4.3: Report generator takes a `List[CheckResult]` and has no knowledge of individual check logic
- NFR-4.4: Adding a new check = adding a new function + calling it from `main.py` — no other files to modify

## Check Metadata Schema

Every check function returns exactly this structure:

```python
{
    "check": str,  # Human-readable name (e.g. "Service Control Policies enabled")
    "description": str,  # One sentence explaining what is verified
    "status": str,  # EXACTLY one of: "complete" | "incomplete" | "error"
    "required": bool,  # true = must-have for well-architected org, false = recommended
    "weight": int,  # Priority 1-6 (6 = critical, 1 = nice-to-have)
    "loe": int,  # Level of effort to fix: 1 = minutes, 6 = weeks
    "remediationLink": str,  # Full URL to AWS documentation for remediation
}
```

When status is "error", an additional field is included:
```python
    "error": str            # Error message (e.g. "AccessDenied")
```

## Well-Architected Foundations Alignment

This assessment maps to the AWS whitepaper "Organizing Your AWS Environment Using Multiple Accounts" (April 2025). Each check traces to a specific best practice:

| Check | Well-Architected Best Practice | Category |
|-------|-------------------------------|----------|
| FR-3.1 Org Exists | "Use multiple AWS accounts" | Foundational |
| FR-3.2 Management Account | "Designate a management account" | Foundational |
| FR-3.3 Minimum 4 Accounts | "At minimum: Management, Log Archive, Audit, 1+ workload" | Foundational |
| FR-3.4 Log Archive Account | "Centralized logging account" (Security OU) | Foundational |
| FR-3.5 Audit Account | "Security Tooling / Audit account" (Security OU) | Foundational |
| FR-3.6 SCP Enabled | "Apply guardrails using SCPs" | Foundational |
| FR-3.7 Tag Policy | "Enforce consistent tagging" | Operational Excellence |
| FR-3.8 Backup Policy | "Centralized backup governance" | Reliability |
| FR-3.9 RCP Enabled | "Resource Control Policies for data perimeter" | Security |
| FR-3.10 Security OU | Foundational OU — required per whitepaper | Foundational |
| FR-3.11 Workloads OU | Application OU — "house business workloads" | Application |
| FR-3.12 Infrastructure OU | Foundational OU — "shared infrastructure services" | Foundational |
| FR-4.1 CloudTrail Org | "Organization-wide audit trail" | Security |
| FR-4.2 Config Org | "Organization-wide resource configuration" | Security |
| FR-4.3 GuardDuty Org | "Organization-wide threat detection" | Security |
| FR-4.4 Security Hub Org | "Centralized security findings" | Security |
| FR-4.5 Access Analyzer Org | "Cross-account access analysis" | Security |
| FR-4.6 RAM Org | "Resource sharing across accounts" | Operational Excellence |
| FR-4.7 IAM IDC Org | "Centralized identity management" | Security |
| FR-4.8 CloudFormation Org | "Organization-wide infrastructure deployment" | Operational Excellence |
| FR-4.9 Backup Org | "Centralized backup management" | Reliability |
| FR-4.10 Cost Opt Hub Org | "Organization-wide cost visibility" | Cost Optimization |
| FR-5.1-5.3 Control Tower | "Automated multi-account governance" | Operational Excellence |
| FR-6.1 IAM IDC Configured | "Use centralized identity, not IAM users" | Security |
| FR-6.2 No IAM Users | "Remove IAM users from management account" | Security |
| FR-7.1-7.2 CloudTrail | "Enable audit logging" | Security |
| FR-7.3-7.4 Config | "Track resource configuration changes" | Security |
| FR-7.5 No EC2 in Mgmt | "Management account has no workloads" | Security |
| FR-7.6 No VPCs in Mgmt | "Management account has no workloads" | Security |
| FR-7.7 CUR | "Enable cost visibility" | Cost Optimization |
| FR-8.1 StackSets | "Deploy consistently across accounts" | Operational Excellence |

### Checks from the whitepaper not yet in WAFA (candidates for future addition)

Based on the **AWS Well-Architected Foundations** document (March 2026, 292 pages, 7 capabilities, 43 best practices), WAFA currently covers ~37% (16/43). Below are the gaps prioritized by customer impact and implementation feasibility.

#### Priority 1 — High value, automatable via API (Phase 2)

| WA Best Practice | What to check | API needed |
|---|---|---|
| FND02-BP01: Protect root users | Root user has no access keys, MFA enabled | `iam.getAccountSummary()` → `AccountAccessKeysPresent`, `AccountMFAEnabled` |
| FND02-BP02: Strong sign-in (MFA) | MFA enforced for IAM users (if any exist) | `iam.listMFADevices()` per user |
| FND03-BP02: Cross-account observability | Config Aggregator exists | `config.describeConfigurationAggregators()` |
| FND05-BP01: Emergency access | Break-glass role or account exists | Check for role named `*emergency*` or `*break-glass*` |
| FND01-BP04: Sandbox/Suspended OUs | Suspended OU and/or Sandbox OU exist | Already detected by WAFA (just not reported) |
| FND05-BP10: Encryption standards | KMS key policy exists, S3 default encryption | `kms.listKeys()`, `s3.getBucketEncryption()` |

#### Priority 2 — Medium value, requires multi-account scan (Phase 2+)

| WA Best Practice | What to check | Notes |
|---|---|---|
| FND02-BP05: Least privilege | Overly permissive policies (admin access) | Requires scanning member accounts |
| FND02-BP08: Credential rotation | Access keys older than 90 days | `iam.listAccessKeys()` + age check |
| FND05-BP05: Non-compliant resources | Config non-compliant rule count | `config.getComplianceSummaryByConfigRule()` |
| FND04-BP05: Service quota management | Service Quotas configured/monitored | `service-quotas.listServiceQuotas()` |
| FND05-BP07: Vulnerability management | Inspector enabled | `inspector2.listMembers()` or org service check |

#### Priority 3 — Lower value or hard to automate (Phase 3+)

| WA Best Practice | Why deferred |
|---|---|
| FND02-BP04: Lifecycle-based access | Process check, not API-automatable |
| FND02-BP07: Data perimeter | Complex — requires VPC endpoint policy analysis |
| FND02-BP10: Machine identity federation | Workload-specific, not org-level |
| FND03-BP01: Observability standards | Policy/process, not automatable |
| FND04-BP03: Shared patterns/repos | Organizational, not API-checkable |
| FND04-BP04: Patch management | Requires SSM inventory across accounts |
| FND04-BP07: Cross-Region failover | Architecture-specific, not generalizable |
| FND04-BP08: Resilience testing | Process check (FIS configured, etc.) |
| FND05-BP09: Forensics capabilities | Requires dedicated forensics account check |
| FND06-BP04: License optimization | License Manager — niche |
| FND06-BP05: Org optimization metrics | CUR analysis — separate tool |
| FND06-BP06: Commitment discounts | Savings Plans/RI — separate tool |
| FND07-BP01-03: Networking | TGW/VPC/DNS — architecture-specific, not one-size-fits-all |

#### Coverage Roadmap

| Phase | Coverage | Best Practices |
|---|---|---|
| **Phase 1 (now)** | 37% (16/43) | WAFA baseline — org structure, services, Control Tower, basic hygiene |
| **Phase 2 (fast-follow)** | ~56% (24/43) | + root user, MFA, Config aggregator, emergency access, encryption, Sandbox/Suspended OUs, multi-account scanning |
| **Phase 3 (future)** | ~70% (30/43) | + credential rotation, least privilege analysis, vulnerability management, service quotas, non-compliant resources |
| **Not automatable** | Remaining ~30% | Process/policy checks that require human judgment |

**Decision:** Phase 1 establishes the WAFA baseline. Priority 1 gaps become Phase 2 targets (most are single API calls, easy to add once the framework exists).

## Testing Strategy

### Unit Tests (run locally, no AWS credentials needed)

- Use `pytest` with `moto` or `unittest.mock` to mock boto3 responses
- Each check function gets its own test file
- Test cases per check:
  - Happy path: API returns data → check returns "complete"
  - Negative path: API returns empty/missing → check returns "incomplete"
  - Error path: API throws exception → check returns "error" with message
  - Edge cases: pagination, partial data, unexpected formats

```
tests/
└── unit/
    ├── test_discovery.py          # FR-2: account type detection, region discovery
    ├── test_organization.py       # FR-3: org governance checks
    ├── test_org_services.py       # FR-4: service integration checks
    ├── test_control_tower.py      # FR-5: Control Tower checks
    ├── test_identity.py           # FR-6: IAM/IDC checks
    ├── test_account_resources.py  # FR-7 + FR-8: per-account resource checks and StackSets org access
    ├── test_delegated_admin.py    # FR-9: delegated administrator discovery
    ├── test_maturity_scoring.py   # FR-11: level calculation logic
    ├── test_report_generation.py  # FR-10: HTML/CSV/JSON output
    └── test_main.py               # main orchestration: account-type routing and report output
```

### Unit Test Pattern

```python
# tests/unit/test_organization.py
from unittest.mock import patch, MagicMock
from src.checks.organization import check_scp_enabled


@patch("src.checks.organization.boto3.client")
def test_scp_enabled_complete(mock_client):
    mock_client.return_value.list_roots.return_value = {
        "Roots": [
            {
                "Id": "r-abc",
                "PolicyTypes": [
                    {"Type": "SERVICE_CONTROL_POLICY", "Status": "ENABLED"}
                ],
            }
        ]
    }
    result = check_scp_enabled()
    assert result["status"] == "complete"
    assert result["check"] == "Service Control Policies enabled"


@patch("src.checks.organization.boto3.client")
def test_scp_enabled_incomplete(mock_client):
    mock_client.return_value.list_roots.return_value = {
        "Roots": [{"Id": "r-abc", "PolicyTypes": []}]
    }
    result = check_scp_enabled()
    assert result["status"] == "incomplete"
```

### Manual Live Validation (requires real AWS credentials)

- There is no automated integration suite; unit tests mock all AWS calls and need no credentials
- Validate against a real management account with an org configured by running `./run-local.sh` (or `python -m src.main`)
- Validates: real API responses parse correctly, pagination works, permissions sufficient
- NOT run in CI/CD — only by a developer with explicit authorization before release

### When to Write Tests

- Kiro writes unit tests ALONGSIDE each check implementation (not after)
- Every check function ships with at least 3 test cases (complete, incomplete, error)
- Maturity scoring tests cover all 5 levels with fixture data
- Report generation tests verify HTML contains expected elements (chart, table, maturity badge)

### Test Dependencies

```
pytest>=7.0
moto>=5.0  # AWS service mocking
pytest-cov  # Coverage reporting
```

## AWS API Permissions Required

Exact actions needed for the assessment IAM role (the deployment role has separate infrastructure permissions):

```
sts:GetCallerIdentity
organizations:DescribeOrganization
organizations:ListAWSServiceAccessForOrganization
organizations:ListRoots
organizations:ListAccounts
organizations:ListOrganizationalUnitsForParent
organizations:ListAccountsForParent
organizations:ListPolicies
organizations:ListTargetsForPolicy
organizations:ListDelegatedAdministrators
organizations:ListDelegatedServicesForAccount
controltower:ListLandingZones
controltower:GetLandingZone
cur:DescribeReportDefinitions
bcm-data-exports:ListExports
cloudtrail:DescribeTrails
cloudtrail:GetTrailStatus
config:DescribeConfigurationRecorders
config:DescribeConfigurationRecorderStatus
config:DescribeDeliveryChannels
config:DescribeDeliveryChannelStatus
ec2:DescribeInstances
ec2:DescribeVpcs
ec2:DescribeRegions
iam:ListUsers
sso:ListInstances
cloudformation:DescribeOrganizationsAccess
s3:PutObject
s3:GetBucketAcl
s3:GetBucketLocation
s3:GetBucketVersioning
s3:GetObject
s3:GetObjectVersion
s3:GetObjectTagging
s3:GetObjectVersionTagging
logs:CreateLogGroup
logs:CreateLogStream
logs:PutLogEvents
sns:Publish  # only when the SNS notification topic is enabled
```

Note: the assessment checks themselves are read-only. The role's write
permissions are scoped to WAFA-owned resources only:

- `s3:PutObject` — upload reports to the WAFA results bucket
- `logs:CreateLogGroup` / `logs:CreateLogStream` / `logs:PutLogEvents` —
  CodeBuild service logs in the `/aws/codebuild/<stack-name>` log group
- `sns:Publish` — assessment notifications to the WAFA SNS topic, granted only
  when the topic is enabled
