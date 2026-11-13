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

from src.report.policy_labels import (
    lookup_error_scope,
    pending_policy_type_warning,
    policy_type_label,
)

# A spreadsheet treats a cell starting with any of these as a formula, so a
# value such as "=cmd|' /c calc'!A0" taken from an AWS resource name would
# execute on open. Prefixing with an apostrophe makes the cell literal text.
_FORMULA_TRIGGERS = ("=", "+", "-", "@")

# Leading whitespace does not stop a spreadsheet from parsing a formula, so
# " =HYPERLINK(...)" is the same payload as "=HYPERLINK(...)". Strip these
# before looking for a trigger.
_LEADING_NOISE = " \t\r\n\v\f\u00a0"

# A cell that *starts* with one of these is escaped whatever follows it: a tab
# or a carriage return at the front of a cell is a row and column separator in
# its own right, so it can break a row apart rather than just carry a formula.
_STRUCTURE_TRIGGERS = ("\t", "\r", "\n")


def _is_number(text):
    """True when the cell is a plain number, formula triggers notwithstanding.

    Keeps the escaping from touching a legitimate negative or signed numeric
    value, which a reader would otherwise find altered relative to the raw
    JSON for no security benefit.
    """
    try:
        float(text)
    except ValueError:
        return False
    return True


def _csv_safe(value):
    """Neutralize spreadsheet formula injection in a single cell.

    Applies to every CSV this module writes: AWS-provided text reaches both
    files — OU and account names in the organization map, and API error
    messages in either — and an escaped cell is the intended, visible
    difference from the raw JSON for a value that would otherwise execute.
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    if text.startswith(_STRUCTURE_TRIGGERS):
        return "'" + text
    stripped = text.lstrip(_LEADING_NOISE)
    if not stripped.startswith(_FORMULA_TRIGGERS):
        return text
    if _is_number(stripped):
        return text
    return "'" + text


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
    """Warnings a reader must see before trusting the map as a whole (FR-12.3).

    A policy lookup fails per policy type or per policy, and a policy type
    can be mid-transition on the root, so neither has a row of its own and
    the message belongs here.

    A failed node listing and a walk stopped at the nesting limit do have a
    row. Those are summarized here as a count and the affected paths, and the
    message itself stays in that row's own Error cell. Someone who opens only
    the spreadsheet then cannot read a partial map as a complete one, and no
    condition is spelled out twice.
    """
    warnings = [
        f"Attached policies may be incomplete for {lookup_error_scope(failure)}: "
        f"{failure.get('error', '')}"
        for failure in org_map.get("policyLookupErrors") or []
    ]
    warnings.extend(
        pending_policy_type_warning(pending)
        for pending in org_map.get("policyTypesPending") or []
    )
    listing_errors = org_map.get("nodeListingErrorPaths") or []
    if listing_errors:
        warnings.append(
            f"Incomplete: {len(listing_errors)} node(s) could not be fully "
            f"listed, see their own Error cell ({', '.join(listing_errors)})"
        )
    truncated = org_map.get("depthTruncatedPaths") or []
    if truncated:
        warnings.append(
            f"Incomplete: the walk stopped below {len(truncated)} node(s), "
            f"see their own Error cell ({', '.join(truncated)})"
        )
    return warnings


_ORG_MAP_HEADER = [
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

# Type cell of the single row written when a run produced no map.
ORG_MAP_UNAVAILABLE_TYPE = "Not available"


def generate_org_map_csv(org_map, rows, unavailable_reason=""):
    """Generate CSV content for the organization and OU account map.

    One row per root, OU, and account. Policy columns list only the policies
    attached *directly* to that row's target, never inherited ones, so an empty
    cell means "nothing attached here" rather than "nothing applies".

    Every reason the map may be incomplete reaches the root row's Error cell,
    so the spreadsheet never presents a partial map as a complete one. A
    condition that belongs to one row is summarized there as a count plus the
    affected paths, and explained in that row's own Error cell.

    Args:
        org_map: the map dict from ``src.checks.org_map.get_organization_map``
        rows: the flattened rows from ``flatten_rows``. Passed in so the
            console, this CSV, and the HTML report all render the same rows
            from one flattening per run.
        unavailable_reason: why this run has no map, written when ``rows``
            is empty.

    Returns:
        str: CSV content. Without a map it is the header plus one row stating
        why, never empty: the file is written on every run so a report
        directory or S3 prefix can never keep an earlier run's map beside
        this run's other reports.
    """
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(_ORG_MAP_HEADER)

    if not rows:
        _write_row(
            writer,
            [ORG_MAP_UNAVAILABLE_TYPE]
            + [""] * (len(_ORG_MAP_HEADER) - 2)
            + [unavailable_reason],
        )
        return output.getvalue()

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
