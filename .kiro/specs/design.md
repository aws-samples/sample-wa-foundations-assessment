# WAFA Assessment — Design Specification

## Architecture Options

### Option A: Local or CloudShell execution

Customer runs `python -m src.main --output ./reports` with local credentials.

- Zero infrastructure to deploy
- Customer needs the read-only assessment permissions
- Output: terminal + HTML/CSV/JSON files in the selected local directory

### Option B: CloudFormation + CodeBuild

```
Customer deploys a CloudFormation stack (one-click)
  → Deployment script uploads source to S3
  → Creates: IAM Role + CodeBuild + results S3 Bucket
  → CodeBuild runs automatically
  → Report lands in S3
  → Can be rerun at any time
```

- One-time deployment
- No ongoing customer effort
- Results in S3 (shareable, persistent, versioned)
- Manual re-runs use CodeBuild's Start build action

### Recommended: Support Both

The Python code is the same; only the output destination and execution wrapper differ:

```
wafa/
├── src/
│   ├── main.py              # Entry point (works in both CloudShell and CodeBuild)
│   ├── checks/
│   │   ├── organization.py  # Org governance checks (FR-3)
│   │   ├── org_services.py  # Org service integrations (FR-4)
│   │   ├── control_tower.py # Control Tower (FR-5)
│   │   ├── identity.py      # IAM/IDC (FR-6)
│   │   ├── account_resources.py  # CloudTrail/Config/EC2/VPC (FR-7, FR-8)
│   │   ├── delegated_admin.py # Delegated administrators (FR-9, informational)
│   │   └── maturity.py        # Maturity scoring (FR-11)
│   ├── discovery/
│   │   ├── account_type.py  # Detect mgmt vs member vs standalone
│   │   └── regions.py       # Discover enabled regions
│   └── report/
│       ├── html_report.py   # HTML shell + JSON report data (Jinja2 sandbox)
│       ├── csv_export.py    # Jira/Asana importable CSV
│       └── assets/          # Prebuilt Cloudscape UI bundle (generated)
├── report-ui/               # Cloudscape React source for the HTML report
├── deployment/
│   └── wafa-stack.yaml      # CloudFormation template (Option B)
├── buildspec.yml            # CodeBuild instructions (Option B)
├── run-local.sh             # Local/CloudShell quick start (Option A)
├── requirements.txt
├── sample-reports/
│   └── wafa-report.html
└── README.md
```

## Execution Modes

### Local or CloudShell Mode (Option A)

```bash
# Customer runs from the repository:
pip install -r requirements.txt
python -m src.main --output ./reports
```

`run-local.sh` does:
```bash
#!/bin/bash
pip install -q -r requirements.txt
python -m src.main --output .
# Reports printed to terminal + saved locally
```

### CodeBuild Mode (Option B)

`buildspec.yml`:
```yaml
version: 0.2
phases:
  install:
    runtime-versions:
      python: 3.12
    commands:
      - pip install -r requirements.txt
  build:
    commands:
      - python -m src.main --output s3://$RESULTS_BUCKET/
```

## Implementation Pattern

### main.py

```python
import argparse
from discovery.account_type import detect_account_type, detect_partition
from discovery.regions import get_enabled_regions
from checks import organization, org_services, control_tower, identity, account_resources
from report.html_report import generate_html
from report.csv_export import generate_csv

def run():
    args = parse_args()  # --output (local directory or s3:// URI)
    _configure_aws_retries()

    # Discover account, partition, and enabled regions
    partition_info = detect_partition()
    account_info = detect_account_type()
    regions = get_enabled_regions(partition_info["default_region"])

    checks = [
        organization.check_org_exists(),
        organization.check_management_account(account_info),
    ]

    if account_info["is_management_account"]:
        checks += organization.run_all()
        checks += org_services.run_all()
        checks += control_tower.run_all()
        checks += identity.run_all(regions, is_management_account=True)
        checks += account_resources.run_all(
            regions,
            is_management_account=True,
            default_region=partition_info["default_region"],
        )
    else:
        checks += identity.run_all(regions, is_management_account=False)
        checks += account_resources.run_all(
            regions,
            is_management_account=False,
            default_region=partition_info["default_region"],
        )

    # Informational, management account only, never scored (FR-9, FR-12)
    #   delegated_admins, delegated_admins_error = get_delegated_administrators()
    #   org_map, org_map_error = get_organization_map()

    # Calculate maturity and generate reports
    maturity = calculate_maturity_level(checks)
    # main.py writes wafa-report.html, wafa-checks.csv, and wafa-raw.json, plus
    # wafa-org-map.csv whenever an organization map was built (FR-12.4)
    
    # Print summary to terminal
    print_summary(checks, maturity)
```

### Check Module Pattern

```python
# src/checks/organization.py
import boto3

def check_scp_enabled():
    client = boto3.client("organizations")
    try:
        resp = client.list_roots()
        roots = resp.get("Roots", [])
        enabled = any(
            pt["Type"] == "SERVICE_CONTROL_POLICY" and pt["Status"] == "ENABLED"
            for root in roots
            for pt in root.get("PolicyTypes", [])
        )
        return {
            "check": "Service Control Policies enabled",
            "description": "SCP should be enabled within the AWS Organization.",
            "status": "complete" if enabled else "incomplete",
            "required": True,
            "weight": 6,
            "loe": 1,
            "remediationLink": "https://docs.aws.amazon.com/organizations/latest/userguide/orgs_manage_policies_enable-disable.html"
        }
    except Exception as e:
        return {"check": "Service Control Policies enabled", "status": "error", "error": str(e), ...}
```

## CloudFormation Template (Option B)

The deployed template is `deployment/wafa-stack.yaml`. The deployment script
first creates the source bucket
`wa-foundations-source-{account_id}-{region}` and uploads
`wa-foundations-source.zip`, then passes `SourceBucket` and `SourceKey` to
CloudFormation.

The stack creates:

1. An encrypted, versioned, public-access-blocked results bucket.
2. A CodeBuild project using an S3 source and the `aws/codebuild/standard:7.0` image.
3. An explicit-action IAM role for assessment APIs, source reads, report writes, and logs.
4. An optional SNS topic/subscription when `EmailAddress` is supplied.
5. An inline Python 3.12 Lambda Custom Resource that starts the first build by
   default for direct CloudFormation deployments. `deploy.sh` disables that
   trigger, starts one build directly after deployment, and waits on the build
   ID returned by `StartBuild`.

The assessment role uses read-only AWS service APIs plus `s3:PutObject` for report
uploads. The source bucket permissions are limited to the named source bucket.
The template does not use a GitHub source or wildcard service-action patterns.

## Phase 2 (Fast-Follow): Multi-Account Scanning

After Phase 1 ships, extend to verify compliance in every member account:

- Deploy read-only role to all member accounts via StackSets
- CodeBuild assumes cross-account role per member account
- Per-account checks: CloudTrail running, Config active, GuardDuty enabled, default VPC deleted
- Refactor to Lambda + Step Functions for parallel execution (50+ accounts)
- Produces per-account findings in the same HTML report
- This is where the sample-aiml pattern (SAM + Lambda + Step Functions) becomes justified

## Dependencies

```
boto3==1.42.97
jinja2==3.1.6
```
