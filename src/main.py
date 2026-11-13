"""WAFA — Well-Architected Foundations Assessment — Main Orchestrator.

Entry point that works in both CloudShell and CodeBuild modes.
Calls discovery, then runs checks based on account type, generates reports.
"""

import argparse
import json
import os
import sys

import boto3

from src.checks import (
    account_resources,
    control_tower,
    identity,
    org_api,
    org_services,
    organization,
)
from src.checks.delegated_admin import get_delegated_administrators
from src.checks.maturity import calculate_maturity_level
from src.checks.org_map import flatten_rows, get_organization_map
from src.discovery.account_type import detect_account_type, detect_partition
from src.discovery.regions import discover_regions
from src.report.csv_export import generate_csv, generate_org_map_csv
from src.report.html_report import generate_html
from src.report.policy_labels import (
    lookup_error_scope,
    pending_policy_type_warning,
    policy_type_label,
)

# Upper bound on organization map warning lines printed to the console, so a
# large or throttled organization cannot bury the assessment results.
MAX_ORG_MAP_CONSOLE_WARNINGS = 10


def parse_args():
    parser = argparse.ArgumentParser(
        description="Well-Architected Foundations Assessment (WAFA)"
    )
    parser.add_argument(
        "--output",
        default=".",
        help="Output directory or S3 URI (s3://bucket/prefix/) for reports",
    )
    return parser.parse_args()


def print_summary(checks, maturity):
    """Print console summary table (FR-10.1)."""
    total = len(checks)
    complete = sum(1 for c in checks if c["status"] == "complete")
    incomplete = sum(1 for c in checks if c["status"] == "incomplete")
    errors = sum(1 for c in checks if c["status"] == "error")
    pct = (complete / total * 100) if total > 0 else 0

    print("\n" + "=" * 80)
    print("WAFA ASSESSMENT RESULTS")
    print("=" * 80)
    print(f"\nMaturity Level: {maturity['level']} — {maturity['name']}")
    print(f"  {maturity['description']}")
    if maturity["next_level"]:
        print(
            f"\n  Progress toward Level {maturity['next_level']}: {maturity['next_level_progress']:.0f}%"
        )
        if maturity["next_level_checks_needed"]:
            print("  Next steps (highest impact first):")
            for name in maturity["next_level_checks_needed"][:5]:
                print(f"    • {name}")

    print(
        f"\nTotal Checks: {total} | Complete: {complete} | Incomplete: {incomplete} | Errors: {errors}"
    )
    print(f"Completion: {pct:.0f}%\n")

    # Table header
    print(f"{'Check':<50} {'Status':<12} {'Required':<10} {'LoE':<5}")
    print("-" * 80)
    for c in checks:
        name = c["check"][:48]
        status = c["status"]
        required = "Yes" if c.get("required") else "No"
        loe = str(c.get("loe", ""))
        print(f"{name:<50} {status:<12} {required:<10} {loe:<5}")

    print("-" * 80)


def print_org_map_summary(org_map, org_map_error, rows):
    """Print a console summary of the organization map (FR-12.5).

    Reasons the map could be incomplete are printed, not just the totals: the
    counts alone would otherwise read as a complete, smaller organization when
    an OU's accounts or children could not be listed. The warning lines are
    capped at ``MAX_ORG_MAP_CONSOLE_WARNINGS``, with a count of the nodes
    affected and of the withheld lines, because every warning is also in the
    HTML report and the CSV.

    Args:
        org_map: the map dict, or None
        org_map_error: why the map could not be built, or None
        rows: the map's flattened rows from ``flatten_rows``, passed in so the
            console, the CSV, and the HTML report render the same rows
    """
    if org_map_error:
        print(f"  Organization map: could not be built — {org_map_error}")
        return
    if not org_map:
        return
    totals = org_map.get("totals", {})
    attached = sum(len(row.get("policies") or []) for row in rows)
    print(
        f"  Organization map: {totals.get('organizationalUnits', 0)} OUs, "
        f"{totals.get('accounts', 0)} accounts, "
        f"{attached} direct policy attachments"
    )
    not_enabled = org_map.get("policyTypesNotEnabled") or []
    if not_enabled:
        labels = ", ".join(policy_type_label(p) for p in not_enabled)
        print(f"    Policy types not enabled: {labels}")

    # The totals above count what was read, so say plainly how many nodes
    # could not be read before listing the individual warnings, which the cap
    # below may truncate.
    # A node can be in both lists (its account listing failed and the walk
    # stopped below it), so count distinct paths rather than adding lengths.
    incomplete_nodes = len(
        set(org_map.get("nodeListingErrorPaths") or [])
        | set(org_map.get("depthTruncatedPaths") or [])
    )
    if incomplete_nodes:
        print(
            f"    Map is INCOMPLETE: {incomplete_nodes} node(s) could not be "
            "fully listed, so the counts above are lower bounds"
        )

    warnings = [
        f"Attachments may be incomplete for {lookup_error_scope(failure)}: "
        f"{failure['error']}"
        for failure in org_map.get("policyLookupErrors") or []
    ]
    warnings.extend(
        pending_policy_type_warning(pending)
        for pending in org_map.get("policyTypesPending") or []
    )
    # Per-node warnings: a denied ListAccountsForParent, an unreadable child
    # OU, or a walk stopped at the nesting limit. Each names the node it
    # applies to, so the gap is locatable rather than just flagged.
    warnings.extend(
        f"{row.get('path') or row.get('name')}: {row['note']}"
        for row in rows
        if row.get("note")
    )
    # A throttled large organization can produce a warning per node, which
    # would push the check results off-screen. The count is always stated, and
    # every warning is in the HTML report and the CSV in full.
    for warning in warnings[:MAX_ORG_MAP_CONSOLE_WARNINGS]:
        print(f"    {warning}")
    withheld = len(warnings) - MAX_ORG_MAP_CONSOLE_WARNINGS
    if withheld > 0:
        print(
            f"    ... and {withheld} more organization map warning(s); "
            "see the Organization Map section of the HTML report "
            "or wafa-org-map.csv for all of them"
        )


def _org_map_unavailable_reason(account_info, org_map_error):
    """Explain why a run has no organization map, for its CSV."""
    if account_info.get("account_type") == "unknown":
        return (
            "Organization map not built: the account type could not be "
            "determined, so the limited assessment ran."
        )
    if not account_info.get("is_management_account"):
        return (
            "Organization map not built: it runs only from the organization's "
            "management account."
        )
    if org_map_error:
        return f"Organization map could not be built: {org_map_error}"
    return "Organization map not built: this account is not in an AWS Organization."


def upload_to_s3(output_path, account_id, files):
    """Upload report files to S3 if output is an S3 URI."""
    # Parse S3 URI
    bucket = output_path.replace("s3://", "").split("/")[0]
    prefix = "/".join(output_path.replace("s3://", "").split("/")[1:])
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    prefix += f"{account_id}/"

    s3 = boto3.client("s3")
    for filename, content, content_type in files:
        key = f"{prefix}{filename}"
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=content,
            ContentType=content_type,
        )
        print(f"  Uploaded: s3://{bucket}/{key}")


def _configure_aws_retries():
    """Use botocore standard retries for throttling and transient failures."""
    os.environ.setdefault("AWS_RETRY_MODE", "standard")
    os.environ.setdefault("AWS_MAX_ATTEMPTS", "5")


def run():
    """Main assessment orchestration."""
    _configure_aws_retries()
    args = parse_args()

    print("WAFA Assessment starting...")
    print("-" * 40)

    # 1. Discover account type and partition
    partition_info = detect_partition()
    print(f"Partition: {partition_info['partition']}")

    account_info = detect_account_type()
    print(f"Account: {account_info['account_id']} ({account_info['account_type']})")

    # 2. Discover regions
    regions, opted_in_regions = discover_regions(partition_info["default_region"])
    print(f"Regions discovered: {len(regions)} ({len(opted_in_regions)} opted-in)")
    print("-" * 40)

    # 3. Run checks based on account type
    checks = []
    delegated_admins = []
    delegated_admins_error = None
    org_map = None
    org_map_error = None
    org_map_rows = []

    # Organization reads shared across checks are cached for one run, so
    # start from a clean cache.
    org_api.reset_cache()

    # FR-3.1, FR-3.2: Always run
    checks.append(organization.check_org_exists())
    checks.append(organization.check_management_account(account_info))

    if account_info["is_management_account"]:
        print("Running FULL assessment (management account)...")

        # FR-3.3 through FR-3.12: Organization governance
        checks.extend(organization.run_all())

        # FR-4: Organization service integrations
        checks.extend(org_services.run_all())

        # FR-5: Control Tower
        checks.extend(control_tower.run_all(opted_in_regions))

        # FR-6: Identity (management account runs both checks)
        checks.extend(
            identity.run_all(
                regions,
                is_management_account=True,
                default_region=partition_info["default_region"],
            )
        )

        # FR-7 + FR-8: Per-account resources (all + management-only)
        checks.extend(
            account_resources.run_all(
                regions,
                is_management_account=True,
                default_region=partition_info["default_region"],
            )
        )

        # FR-9: Delegated administrators (informational)
        delegated_admins, delegated_admins_error = get_delegated_administrators()

        # FR-12: Organization and OU account map (informational)
        org_map, org_map_error = get_organization_map()
        # Flattened once per run. The console, the CSV, and the HTML report
        # all render these rows, so they cannot disagree.
        org_map_rows = flatten_rows(org_map)
        print_org_map_summary(org_map, org_map_error, org_map_rows)

    else:
        print("Running LIMITED assessment (non-management account)...")
        # FR-2.4: Limited checks for non-management accounts
        checks.extend(
            identity.run_all(
                regions,
                is_management_account=False,
                default_region=partition_info["default_region"],
            )
        )
        checks.extend(
            account_resources.run_all(
                regions,
                is_management_account=False,
                default_region=partition_info["default_region"],
            )
        )

    # 4. Calculate maturity level
    maturity = calculate_maturity_level(checks)

    # 5. Print console summary
    print_summary(checks, maturity)

    # 6. Generate reports
    html_content = generate_html(
        checks,
        maturity,
        delegated_admins,
        account_info,
        delegated_admins_error=delegated_admins_error,
        org_map=org_map,
        org_map_error=org_map_error,
        org_map_rows=org_map_rows,
    )
    csv_content = generate_csv(checks)
    raw_json = json.dumps(
        {
            "account_info": account_info,
            "maturity": maturity,
            "checks": checks,
            "delegated_administrators": delegated_admins,
            "delegated_administrators_error": delegated_admins_error,
            "organization_map": org_map,
            "organization_map_error": org_map_error,
        },
        indent=2,
        default=str,
    )

    report_files = [
        ("wafa-report.html", html_content, "text/html"),
        ("wafa-checks.csv", csv_content, "text/csv"),
        ("wafa-raw.json", raw_json, "application/json"),
    ]

    # FR-12.4: the organization map CSV is written on every run, stating why
    # when there is no map. Skipping it would leave an earlier run's map in
    # the same directory or S3 prefix, where it reads as the current result.
    report_files.append(
        (
            "wafa-org-map.csv",
            generate_org_map_csv(
                org_map,
                org_map_rows,
                _org_map_unavailable_reason(account_info, org_map_error),
            ),
            "text/csv",
        )
    )

    # 7. Output reports
    if args.output.startswith("s3://"):
        print("\nUploading reports to S3...")
        upload_to_s3(args.output, account_info["account_id"], report_files)
    else:
        output_dir = args.output
        os.makedirs(output_dir, exist_ok=True)
        for filename, content, content_type in report_files:
            # Explicit UTF-8: the reports contain non-ASCII text, which would
            # fail to write on a host with a C/POSIX locale.
            #
            # CSV content already carries the \r\n line endings csv.writer
            # produces, so newline="" stops the text layer translating them
            # into \r\r\n on Windows.
            newline = "" if content_type.startswith("text/csv") else None
            path = os.path.join(output_dir, filename)
            with open(path, "w", encoding="utf-8", newline=newline) as f:
                f.write(content)
        print(f"\nReports saved to: {output_dir}/")
        for filename, _, _ in report_files:
            print(f"  {filename}")

    print("\nWAFA Assessment complete.")
    return checks, maturity


def exit_code(checks):
    """Return 1 when every check errored, which usually means missing
    permissions; partial errors still produce a useful report and return 0."""
    if checks and all(c.get("status") == "error" for c in checks):
        return 1
    return 0


if __name__ == "__main__":
    final_checks, _ = run()
    if exit_code(final_checks):
        print(
            "ERROR: Every check returned an error. Review the assessment permissions."
        )
    sys.exit(exit_code(final_checks))
