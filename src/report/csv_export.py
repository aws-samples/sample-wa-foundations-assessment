"""CSV Report Generation (FR-10.2) and organization map export (FR-12.4).

Generates a CSV file with all check results, plus a separate CSV of the
organization and OU account map. The two stay in separate files so that
``wafa-checks.csv`` keeps one row per check.

Both exports pass every cell through ``_csv_safe``, because cells can contain
AWS-provided text: OU and account names in the organization map, and API error
messages in either file.
"""

import csv
import io

from src.checks.org_map import flatten_rows
from src.report.policy_labels import policy_type_label

# A spreadsheet treats a cell starting with any of these as a formula, so a
# value such as "=cmd|' /c calc'!A0" taken from an AWS resource name would
# execute on open. Prefixing with an apostrophe makes the cell literal text.
_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value):
    """Neutralize spreadsheet formula injection in a single cell."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    if text.startswith(_FORMULA_TRIGGERS):
        return "'" + text
    return text


def _write_row(writer, values):
    writer.writerow([_csv_safe(value) for value in values])


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
        _write_row(
            writer,
            [
                check.get("check", ""),
                check.get("description", ""),
                check.get("status", ""),
                "Yes" if check.get("required") else "No",
                check.get("weight", ""),
                check.get("loe", ""),
                check.get("remediationLink", ""),
                check.get("error", ""),
            ],
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


def _map_level_warnings(org_map):
    """Warnings that apply to the whole map rather than one node.

    These reach the CSV so that a reader who opens only the spreadsheet cannot
    mistake an incomplete map for a complete one (FR-12.3).
    """
    warnings = []
    for failure in org_map.get("policyLookupErrors") or []:
        warnings.append(
            f"Attached policies may be incomplete for {failure.get('scope', '')}: "
            f"{failure.get('error', '')}"
        )
    if org_map.get("depthTruncated"):
        warnings.append(
            "OU hierarchy is nested deeper than this map walks; "
            "some OUs and accounts are not listed"
        )
    return warnings


def generate_org_map_csv(org_map):
    """Generate CSV content for the organization and OU account map.

    One row per root, OU, and account. Policy columns list only the policies
    attached *directly* to that row's target, never inherited ones, so an empty
    cell means "nothing attached here" rather than "nothing applies".

    Map-level warnings (failed policy lookups, a truncated hierarchy) are
    written into the root row's Error cell, so the spreadsheet never presents an
    incomplete map as a complete one.

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

    map_warnings = _map_level_warnings(org_map)

    for index, row in enumerate(rows):
        policies = row.get("policies") or []
        notes = [row.get("note", "")] if row.get("note") else []
        if index == 0:
            # The root row carries the map-level warnings.
            notes = map_warnings + notes
        _write_row(
            writer,
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
                "; ".join(notes),
            ],
        )

    return output.getvalue()
