#!/usr/bin/env python3
"""Generate the sanitized sample WAFA report committed under sample-reports/.

This script produces the example reports that let people preview WAFA's output
without running it against a real account (GitHub issue #2).

How it stays faithful (no hand-written report content):
- It calls the *real* orchestrator, ``src.main.run()``. Every check definition,
  the maturity scoring, and the HTML/CSV/JSON rendering come from production
  code in ``src/`` — this script never authors report content itself.
- The only thing it supplies is a mocked set of AWS API responses (patched at
  the ``boto3.client`` seam, exactly like the unit tests do). The mock models a
  *management* account with a realistic, mixed posture — most foundations in
  place, a few gaps, and one permission error — so the sample exercises the full
  report: complete/incomplete/error states, the "next steps" list, the five-level
  maturity ladder and generated scoring criteria, and partial capability coverage
  (rather than an unrealistic all-green Level 5).
- All identifiers are obvious, non-real placeholders (AWS documentation-style
  account IDs and example names), so the committed sample contains no real data.

The HTML sample uses the Cloudscape Design System report UI. Its prebuilt
bundle in ``src/report/assets/`` is inlined by the production renderer, so
after changing ``report-ui/`` rebuild it first (``npm run build`` in
``report-ui/``). After generating, the script verifies that the HTML, CSV, and
JSON agree and that the HTML embeds the current bundle and report data.

Regenerate with:  python -m scripts.generate_sample_report
Output:           sample-reports/{wafa-report.html,wafa-checks.csv,wafa-raw.json,
                  wafa-org-map.csv}
"""

import csv
import io
import json
import os
import sys
from unittest.mock import MagicMock, patch

from src.checks.org_map import flatten_rows
from src.report.csv_export import generate_csv, generate_org_map_csv

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS_DIR = os.path.join(REPO_ROOT, "src", "report", "assets")
UI_ASSETS = ("report-ui.js", "report-ui.css")
REPORT_DATA_MARKER = '<script type="application/json" id="wafa-report-data">'

# Placeholder identifiers — deliberately fake (AWS "documentation" account IDs).
MGMT_ACCOUNT_ID = "111111111111"
LOG_ARCHIVE_ID = "222222222222"
AUDIT_ID = "333333333333"
WORKLOAD_ID = "444444444444"
ORG_ID = "o-example12345"


def _sample_management_account_client_factory():
    """boto3.client factory modelling a well-configured management account.

    Mirrors tests/unit/test_main.py::_mock_management_account_boto3 (the vetted,
    passing fixture) so the sample is structurally identical to a real run.
    """

    def client_factory(service, **kwargs):
        mock = MagicMock()

        if service == "sts":
            mock.get_caller_identity.return_value = {"Account": MGMT_ACCOUNT_ID}

        elif service == "organizations":
            mock.describe_organization.return_value = {
                "Organization": {"Id": ORG_ID, "MasterAccountId": MGMT_ACCOUNT_ID}
            }
            accounts_paginator = MagicMock()
            accounts_paginator.paginate.return_value = [
                {
                    "Accounts": [
                        {"Id": MGMT_ACCOUNT_ID, "Name": "Management"},
                        {"Id": LOG_ARCHIVE_ID, "Name": "Log Archive"},
                        {"Id": AUDIT_ID, "Name": "Audit"},
                        {"Id": WORKLOAD_ID, "Name": "Workload-Prod"},
                    ]
                }
            ]
            svc_paginator = MagicMock()
            # Realistic gap: Security Hub is NOT enabled at the org level, so the
            # "Security Hub organization service enabled" check reports incomplete.
            svc_paginator.paginate.return_value = [
                {
                    "EnabledServicePrincipals": [
                        {"ServicePrincipal": "cloudtrail.amazonaws.com"},
                        {"ServicePrincipal": "config.amazonaws.com"},
                        {"ServicePrincipal": "guardduty.amazonaws.com"},
                        {"ServicePrincipal": "access-analyzer.amazonaws.com"},
                        {"ServicePrincipal": "ram.amazonaws.com"},
                        {"ServicePrincipal": "sso.amazonaws.com"},
                        {
                            "ServicePrincipal": "member.org.stacksets.cloudformation.amazonaws.com"
                        },
                        {"ServicePrincipal": "backup.amazonaws.com"},
                        {"ServicePrincipal": "cost-optimization-hub.bcm.amazonaws.com"},
                    ]
                }
            ]
            # Realistic gap: no Infrastructure OU yet → that check reports
            # incomplete. Keyed by parent so the FR-12 organization map walks a
            # finite hierarchy, with one nested OU to show depth.
            child_ous = {
                "r-root": [
                    {"Id": "ou-sec", "Name": "Security"},
                    {"Id": "ou-wl", "Name": "Workloads"},
                ],
                "ou-wl": [{"Id": "ou-prod", "Name": "Production"}],
            }
            ou_paginator = MagicMock()
            ou_paginator.paginate.side_effect = lambda ParentId: [
                {"OrganizationalUnits": child_ous.get(ParentId, [])}
            ]

            # FR-12: account placement per OU and directly attached policies.
            accounts_for_parent = {
                "r-root": [
                    {"Id": MGMT_ACCOUNT_ID, "Name": "Management", "Status": "ACTIVE"}
                ],
                "ou-sec": [
                    {"Id": LOG_ARCHIVE_ID, "Name": "Log Archive", "Status": "ACTIVE"},
                    {"Id": AUDIT_ID, "Name": "Audit", "Status": "ACTIVE"},
                ],
                "ou-prod": [
                    {"Id": WORKLOAD_ID, "Name": "Workload-Prod", "Status": "ACTIVE"}
                ],
            }
            afp_paginator = MagicMock()
            afp_paginator.paginate.side_effect = lambda ParentId: [
                {"Accounts": accounts_for_parent.get(ParentId, [])}
            ]

            # FR-12.3: policies are enumerated by type, then located by target.
            # BACKUP_POLICY is enabled but has no policies, exercising the
            # "enabled but unused" path.
            policies_by_type = {
                "SERVICE_CONTROL_POLICY": [
                    {
                        "Id": "p-FullAWSAccess",
                        "Name": "FullAWSAccess",
                        "Type": "SERVICE_CONTROL_POLICY",
                        "AwsManaged": True,
                    },
                    {
                        "Id": "p-example-denyroot",
                        "Name": "DenyRootUserActions",
                        "Type": "SERVICE_CONTROL_POLICY",
                        "AwsManaged": False,
                    },
                ],
                "TAG_POLICY": [
                    {
                        "Id": "p-example-tags",
                        "Name": "RequireCostCenterTag",
                        "Type": "TAG_POLICY",
                        "AwsManaged": False,
                    }
                ],
                "BACKUP_POLICY": [],
            }
            lp_paginator = MagicMock()
            lp_paginator.paginate.side_effect = lambda Filter: [
                {"Policies": policies_by_type.get(Filter, [])}
            ]

            # An account-level attachment is included because the inverted
            # lookup returns account targets at no extra cost.
            targets_by_policy = {
                "p-FullAWSAccess": [
                    {"TargetId": "r-root", "Type": "ROOT", "Name": "Root"}
                ],
                "p-example-denyroot": [
                    {
                        "TargetId": "ou-sec",
                        "Type": "ORGANIZATIONAL_UNIT",
                        "Name": "Security",
                    },
                    {
                        "TargetId": WORKLOAD_ID,
                        "Type": "ACCOUNT",
                        "Name": "Workload-Prod",
                    },
                ],
                "p-example-tags": [
                    {
                        "TargetId": "ou-prod",
                        "Type": "ORGANIZATIONAL_UNIT",
                        "Name": "Production",
                    }
                ],
            }
            tfp_paginator = MagicMock()
            tfp_paginator.paginate.side_effect = lambda PolicyId: [
                {"Targets": targets_by_policy.get(PolicyId, [])}
            ]
            da_paginator = MagicMock()
            da_paginator.paginate.return_value = [
                {"DelegatedAdministrators": [{"Id": AUDIT_ID, "Name": "Audit"}]}
            ]
            ds_paginator = MagicMock()
            ds_paginator.paginate.return_value = [
                {"DelegatedServices": [{"ServicePrincipal": "guardduty.amazonaws.com"}]}
            ]

            def get_paginator(name):
                return {
                    "list_accounts": accounts_paginator,
                    "list_aws_service_access_for_organization": svc_paginator,
                    "list_organizational_units_for_parent": ou_paginator,
                    "list_accounts_for_parent": afp_paginator,
                    "list_policies": lp_paginator,
                    "list_targets_for_policy": tfp_paginator,
                    "list_delegated_administrators": da_paginator,
                    "list_delegated_services_for_account": ds_paginator,
                }.get(name, MagicMock())

            mock.get_paginator.side_effect = get_paginator
            # Realistic gap: RCPs not yet enabled → "Resource Control Policies
            # enabled" reports incomplete.
            mock.list_roots.return_value = {
                "Roots": [
                    {
                        "Id": "r-root",
                        "Name": "Root",
                        "PolicyTypes": [
                            {"Type": "SERVICE_CONTROL_POLICY", "Status": "ENABLED"},
                            {"Type": "TAG_POLICY", "Status": "ENABLED"},
                            {"Type": "BACKUP_POLICY", "Status": "ENABLED"},
                        ],
                    }
                ]
            }

        elif service == "controltower":
            mock.list_landing_zones.return_value = {
                "landingZones": [
                    {
                        "arn": "arn:aws:controltower:us-east-1:111111111111:landingzone/lz1"
                    }
                ]
            }
            mock.get_landing_zone.return_value = {
                "landingZone": {
                    "driftStatus": {"status": "IN_SYNC"},
                    "version": "3.3",
                    "latestAvailableVersion": "3.3",
                }
            }

        elif service == "sso-admin":
            mock.list_instances.return_value = {
                "Instances": [{"InstanceArn": "arn:aws:sso:::instance/ssoins-example"}]
            }

        elif service == "iam":
            mock.get_paginator.return_value.paginate.return_value = [{"Users": []}]

        elif service == "ec2":
            mock.describe_regions.return_value = {
                "Regions": [
                    {"RegionName": "us-east-1"},
                    {"RegionName": "us-west-2"},
                ]
            }
            mock.describe_instances.return_value = {"Reservations": []}
            mock.describe_vpcs.return_value = {"Vpcs": []}

        elif service == "cloudtrail":
            mock.get_trail_status.return_value = {"IsLogging": True}
            mock.describe_trails.return_value = {
                "trailList": [
                    {
                        "Name": "org-trail",
                        "IsOrganizationTrail": True,
                        "HomeRegion": "us-east-1",
                    }
                ]
            }

        elif service == "config":
            # Realistic error: the assessment role is denied the Config recorder
            # API, so "AWS Config recorder active" reports an *error* (not a false
            # incomplete) — showing how permission gaps surface in the report.
            from botocore.exceptions import ClientError

            denied = ClientError(
                {
                    "Error": {
                        "Code": "AccessDeniedException",
                        "Message": "User is not authorized to perform: "
                        "config:DescribeConfigurationRecorders",
                    }
                },
                "DescribeConfigurationRecorders",
            )
            mock.describe_configuration_recorders.side_effect = denied
            # Delivery channel is readable and healthy → that check stays complete.
            mock.describe_delivery_channels.return_value = {
                "DeliveryChannels": [{"name": "default"}]
            }
            mock.describe_delivery_channel_status.return_value = {
                "DeliveryChannelsStatus": [
                    {
                        "name": "default",
                        "configHistoryDeliveryInfo": {"lastStatus": "SUCCESS"},
                    }
                ]
            }

        elif service == "cur":
            # Realistic gap: no Cost and Usage Report configured → incomplete.
            mock.describe_report_definitions.return_value = {"ReportDefinitions": []}

        elif service == "bcm-data-exports":
            mock.list_exports.return_value = {"Exports": []}

        elif service == "cloudformation":
            mock.describe_organizations_access.return_value = {"Status": "ENABLED"}

        elif service == "s3":
            mock.put_object.return_value = {}

        return mock

    return client_factory


def _require_ui_assets():
    """Fail early with a clear message if the Cloudscape bundle is missing."""
    missing = [
        name for name in UI_ASSETS if not os.path.isfile(os.path.join(ASSETS_DIR, name))
    ]
    if missing:
        sys.exit(
            f"Missing report UI assets in {ASSETS_DIR}: {', '.join(missing)}. "
            "Run 'npm ci && npm run build' in report-ui/ first."
        )


def _read(path):
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def _verify_org_map_outputs(out_dir, org_map):
    """Check the FR-12 organization map output (returns a problem list)."""
    problems = []
    if not org_map:
        problems.append("wafa-raw.json has no organization_map")
        return problems

    totals = org_map.get("totals", {})
    if not totals.get("organizationalUnits") or not totals.get("accounts"):
        problems.append("organization_map has no OUs or accounts to illustrate")
    if totals.get("maxDepth", 0) < 2:
        problems.append("organization_map does not exercise a nested OU")
    if "RESOURCE_CONTROL_POLICY" not in (org_map.get("policyTypesNotEnabled") or []):
        problems.append("sample no longer exercises a not-enabled policy type")
    if org_map.get("policyLookupErrors"):
        problems.append("sample organization map has policy lookup errors")

    rows = flatten_rows(org_map)
    if not any(row["kind"] == "ou" and row["policies"] for row in rows):
        problems.append("sample has no OU-level policy attachment to illustrate")
    if not any(row["kind"] == "account" and row["policies"] for row in rows):
        problems.append("sample has no account-level policy attachment")

    # The CSV must match what the production generator produces from the same
    # map, so the committed file cannot drift from the code.
    path = os.path.join(out_dir, "wafa-org-map.csv")
    if not os.path.isfile(path):
        problems.append("wafa-org-map.csv was not generated")
    elif _read(path) != generate_org_map_csv(org_map):
        problems.append("wafa-org-map.csv differs from the generated organization map")

    return problems


def _verify_outputs(out_dir):
    """Check the generated sample is complete, consistent, and self-contained."""
    html = _read(os.path.join(out_dir, "wafa-report.html"))
    raw = json.loads(_read(os.path.join(out_dir, "wafa-raw.json")))
    with open(os.path.join(out_dir, "wafa-checks.csv"), encoding="utf-8") as f:
        csv_rows = list(csv.reader(f))
    expected_csv_rows = list(csv.reader(io.StringIO(generate_csv(raw["checks"]))))

    problems = []
    if '<div id="wafa-report-root"></div>' not in html:
        problems.append("HTML is missing the Cloudscape report root element")
    for name in UI_ASSETS:
        if _read(os.path.join(ASSETS_DIR, name)).rstrip("\n") not in html:
            problems.append(f"HTML does not inline the current {name}")
    if REPORT_DATA_MARKER not in html:
        problems.append("HTML is missing the embedded report data")
    else:
        payload = html.split(REPORT_DATA_MARKER, 1)[1].split("</script>", 1)[0]
        data = json.loads(payload)
        if data["checks"] != raw["checks"]:
            problems.append("HTML report data checks differ from wafa-raw.json")
        if data["maturity"] != raw["maturity"]:
            problems.append("HTML report data maturity differs from wafa-raw.json")
        for key in (
            "account_info",
            "delegated_administrators",
            "delegated_administrators_error",
            "organization_map",
            "organization_map_error",
        ):
            if key not in data or key not in raw or data[key] != raw[key]:
                problems.append(f"HTML report data {key} differs from wafa-raw.json")
        statuses = {check["status"] for check in data["checks"]}
        if not {"complete", "incomplete", "error"} <= statuses:
            problems.append("sample does not exercise complete, incomplete, and error")

    levels = raw["maturity"].get("scoring_model", {}).get("levels", [])
    if len(levels) != 5:
        problems.append("wafa-raw.json does not include a five-level scoring_model")
    if csv_rows != expected_csv_rows:
        problems.append("wafa-checks.csv data differs from wafa-raw.json checks")
    for name in ("wafa-report.html", "wafa-raw.json"):
        text = _read(os.path.join(out_dir, name))
        if any(line != line.rstrip() for line in text.splitlines()):
            problems.append(f"{name} contains trailing whitespace")

    problems.extend(_verify_org_map_outputs(out_dir, raw.get("organization_map")))

    if problems:
        sys.exit("Sample report verification failed:\n- " + "\n- ".join(problems))
    org_totals = (raw.get("organization_map") or {}).get("totals", {})
    print(
        f"Verified sample: {len(raw['checks'])} checks, maturity level "
        f"{raw['maturity']['level']}, Cloudscape report UI inlined, "
        f"organization map with {org_totals.get('organizationalUnits', 0)} OUs "
        f"and {org_totals.get('accounts', 0)} accounts."
    )


def main():
    out_dir = os.path.join(REPO_ROOT, "sample-reports")
    os.makedirs(out_dir, exist_ok=True)
    _require_ui_assets()

    factory = _sample_management_account_client_factory()

    # Patch every boto3.client seam the assessment uses, set argv to write into
    # sample-reports/, then run the real orchestrator.
    seams = [
        "src.discovery.account_type.boto3.client",
        "src.discovery.regions.boto3.client",
        "src.checks.organization.boto3.client",
        "src.checks.org_services.boto3.client",
        "src.checks.control_tower.boto3.client",
        "src.checks.identity.boto3.client",
        "src.checks.account_resources.boto3.client",
        "src.checks.delegated_admin.boto3.client",
        "src.checks.org_map.boto3.client",
    ]

    from contextlib import ExitStack

    with ExitStack() as stack:
        for seam in seams:
            m = stack.enter_context(patch(seam))
            m.side_effect = factory
        old_argv = sys.argv
        sys.argv = ["generate_sample_report", "--output", out_dir]
        try:
            from src.main import run

            run()
        finally:
            sys.argv = old_argv

    _verify_outputs(out_dir)
    print(f"\nSample reports written to: {out_dir}")


if __name__ == "__main__":
    main()
