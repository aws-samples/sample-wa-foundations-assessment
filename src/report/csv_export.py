"""CSV Report Generation (FR-10.2) and organization map export (FR-12.4).

Generates a CSV file with all check results, plus a separate CSV of the
organization and OU account map. The two stay in separate files so that
``wafa-checks.csv`` keeps one row per check.
"""

import csv
import io

from src.checks.org_map import flatten_rows

# Short display labels for Organizations policy types. Mirrors
# POLICY_TYPE_LABELS in report-ui/src/App.jsx — update both together.
_POLICY_TYPE_LABELS = {
    "SERVICE_CONTROL_POLICY": "SCP",
    "RESOURCE_CONTROL_POLICY": "RCP",
    "TAG_POLICY": "Tag",
    "BACKUP_POLICY": "Backup",
    "AISERVICES_OPT_OUT_POLICY": "AI opt-out",
    "CHATBOT_POLICY": "Chatbot",
    "DECLARATIVE_POLICY_EC2": "Declarative EC2",
    "SECURITYHUB_POLICY": "Security Hub",
    "INSPECTOR_POLICY": "Inspector",
    "UPGRADE_ROLLOUT_POLICY": "Upgrade rollout",
    "BEDROCK_POLICY": "Bedrock",
    "S3_POLICY": "S3",
    "NETWORK_SECURITY_DIRECTOR_POLICY": "Network security",
    "GUARDDUTY_POLICY": "GuardDuty",
}


def policy_type_label(policy_type):
    """Return a short display label for an Organizations policy type."""
    if policy_type in _POLICY_TYPE_LABELS:
        return _POLICY_TYPE_LABELS[policy_type]
    return (policy_type or "").replace("_", " ").title()


def generate_csv(checks):
    """Generate CSV content from check results.

    Args:
        checks: list of check result dicts

    Returns:
        str: CSV content as string
    """
    output = io.StringIO()
    writer = csv.writer(output)

    # Header row
    writer.writerow(
        [
            "Check Name",
            "Description",
            "Status",
            "Required",
            "Weight",
            "Level of Effort",
            "Remediation Link",
            "Error",
        ]
    )

    # Data rows
    for check in checks:
        writer.writerow(
            [
                check.get("check", ""),
                check.get("description", ""),
                check.get("status", ""),
                "Yes" if check.get("required") else "No",
                check.get("weight", ""),
                check.get("loe", ""),
                check.get("remediationLink", ""),
                check.get("error", ""),
            ]
        )

    return output.getvalue()


_ROW_TYPE_LABELS = {"root": "Root", "ou": "OU", "account": "Account"}


def _names_of_type(policies, policy_type):
    """Return the comma-joined names of attached policies of one type."""
    return ", ".join(
        policy.get("name", "")
        for policy in policies
        if policy.get("type") == policy_type
    )


def _other_policy_names(policies):
    """Summarize attached policies other than SCPs and RCPs."""
    grouped = {}
    for policy in policies:
        policy_type = policy.get("type", "")
        if policy_type in ("SERVICE_CONTROL_POLICY", "RESOURCE_CONTROL_POLICY"):
            continue
        grouped.setdefault(policy_type, []).append(policy.get("name", ""))
    return "; ".join(
        f"{policy_type_label(policy_type)}: {', '.join(names)}"
        for policy_type, names in sorted(grouped.items())
    )


def generate_org_map_csv(org_map):
    """Generate CSV content for the organization and OU account map.

    One row per root, OU, and account. Policy columns list only the policies
    attached *directly* to that row's target, never inherited ones, so an empty
    cell means "nothing attached here" rather than "nothing applies".

    Args:
        org_map: the map dict from ``src.checks.org_map.get_organization_map``

    Returns:
        str: CSV content, or an empty string when there is no map.
    """
    rows = flatten_rows(org_map)
    if not rows:
        return ""

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "Type",
            "OU Path",
            "Name",
            "ID",
            "Status",
            "Depth",
            "Directly Attached SCPs",
            "Directly Attached RCPs",
            "Other Directly Attached Policies",
            "Error",
        ]
    )

    for row in rows:
        policies = row.get("policies") or []
        writer.writerow(
            [
                _ROW_TYPE_LABELS.get(row.get("kind"), row.get("kind", "")),
                row.get("path", ""),
                row.get("name", ""),
                row.get("id", ""),
                row.get("status", ""),
                row.get("depth", ""),
                _names_of_type(policies, "SERVICE_CONTROL_POLICY"),
                _names_of_type(policies, "RESOURCE_CONTROL_POLICY"),
                _other_policy_names(policies),
                row.get("note", ""),
            ]
        )

    return output.getvalue()
