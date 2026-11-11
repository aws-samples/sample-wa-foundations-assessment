"""Unit tests for the FR-12.4 organization map CSV in src/report/csv_export.py."""

import csv
import io

import pytest

from src.report.csv_export import generate_csv, generate_org_map_csv
from src.report.policy_labels import policy_type_label


def account(account_id, name, status="ACTIVE", policies=None):
    return {
        "id": account_id,
        "name": name,
        "status": status,
        "policies": policies or [],
    }


def org_map(root=None, **overrides):
    data = {
        "organizationId": "o-example12345",
        "managementAccountId": "111111111111",
        "enabledPolicyTypes": ["RESOURCE_CONTROL_POLICY", "SERVICE_CONTROL_POLICY"],
        "policyTypesNotEnabled": [],
        "policyLookupErrors": [],
        "totals": {"organizationalUnits": 2, "accounts": 3, "maxDepth": 2},
        "root": root
        if root is not None
        else {
            "id": "r-root",
            "name": "Root",
            "type": "ROOT",
            "path": "Root",
            "depth": 0,
            "policies": [
                {
                    "id": "p-full",
                    "name": "FullAWSAccess",
                    "type": "SERVICE_CONTROL_POLICY",
                    "awsManaged": True,
                }
            ],
            "accounts": [account("111111111111", "Management")],
            "children": [
                {
                    "id": "ou-sec",
                    "name": "Security",
                    "type": "ORGANIZATIONAL_UNIT",
                    "path": "Root/Security",
                    "depth": 1,
                    "policies": [
                        {
                            "id": "p-deny",
                            "name": "DenyRootUser",
                            "type": "SERVICE_CONTROL_POLICY",
                            "awsManaged": False,
                        }
                    ],
                    "accounts": [
                        account(
                            "333333333333",
                            "Audit",
                            status="SUSPENDED",
                            policies=[
                                {
                                    "id": "p-acct",
                                    "name": "AccountGuardrail",
                                    "type": "SERVICE_CONTROL_POLICY",
                                    "awsManaged": False,
                                }
                            ],
                        )
                    ],
                    "children": [
                        {
                            "id": "ou-log",
                            "name": "Logging",
                            "type": "ORGANIZATIONAL_UNIT",
                            "path": "Root/Security/Logging",
                            "depth": 2,
                            "policies": [],
                            "accounts": [account("222222222222", "Log Archive")],
                            "children": [],
                        }
                    ],
                }
            ],
        },
    }
    data.update(overrides)
    return data


def rows_for(data):
    return list(csv.reader(io.StringIO(generate_org_map_csv(data))))


class TestPolicyTypeLabel:
    def test_maps_known_types_to_short_labels(self):
        assert policy_type_label("SERVICE_CONTROL_POLICY") == "SCP"
        assert policy_type_label("RESOURCE_CONTROL_POLICY") == "RCP"
        assert policy_type_label("DECLARATIVE_POLICY_EC2") == "Declarative EC2"

    def test_falls_back_to_a_readable_label(self):
        assert policy_type_label("SOME_NEW_POLICY") == "Some New Policy"
        assert policy_type_label("") == ""


class TestOrgMapCsv:
    def test_header_and_one_row_per_target(self):
        rows = rows_for(org_map())
        assert rows[0] == [
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
        assert [row[0] for row in rows[1:]] == [
            "Root",
            "Account",
            "OU",
            "Account",
            "OU",
            "Account",
        ]

    def test_separates_scp_rcp_and_other_policy_columns(self):
        root = org_map()["root"]
        root["policies"] = [
            {"id": "p-1", "name": "DenyRoot", "type": "SERVICE_CONTROL_POLICY"},
            {"id": "p-2", "name": "Perimeter", "type": "RESOURCE_CONTROL_POLICY"},
            {"id": "p-3", "name": "RequireTags", "type": "TAG_POLICY"},
            {"id": "p-4", "name": "EC2Defaults", "type": "DECLARATIVE_POLICY_EC2"},
        ]
        root_row = rows_for(org_map(root=root))[1]
        assert root_row[6] == "DenyRoot"
        assert root_row[7] == "Perimeter"
        assert root_row[8] == "Declarative EC2: EC2Defaults; Tag: RequireTags"

    def test_records_account_placement_and_status(self):
        rows = rows_for(org_map())
        audit = next(row for row in rows if row[2] == "Audit")
        assert audit[1] == "Root/Security"
        assert audit[3] == "333333333333"
        assert audit[4] == "SUSPENDED"
        assert audit[5] == "2"

    def test_includes_account_level_attachments(self):
        audit = next(row for row in rows_for(org_map()) if row[2] == "Audit")
        assert audit[6] == "AccountGuardrail"

    def test_reports_node_errors(self):
        root = org_map()["root"]
        root["accountsError"] = "AccessDenied listing accounts"
        assert rows_for(org_map(root=root))[1][9] == "AccessDenied listing accounts"

    def test_quotes_names_containing_separators(self):
        root = org_map()["root"]
        root["name"] = 'Prod, "Core"'
        root["path"] = 'Prod, "Core"'
        content = generate_org_map_csv(org_map(root=root))
        # Round-trips through the csv module without corrupting the row.
        parsed = list(csv.reader(io.StringIO(content)))
        assert parsed[1][2] == 'Prod, "Core"'

    def test_empty_without_a_map(self):
        assert generate_org_map_csv(None) == ""
        assert generate_org_map_csv({}) == ""


class TestFormulaInjection:
    """OU and account names are AWS-provided and land in spreadsheet cells."""

    @pytest.mark.parametrize("trigger", ["=", "+", "-", "@", "\t", "\r"])
    def test_neutralizes_a_leading_formula_character_in_a_name(self, trigger):
        root = org_map()["root"]
        root["name"] = f"{trigger}cmd|' /c calc'!A0"
        name_cell = rows_for(org_map(root=root))[1][2]
        assert name_cell.startswith("'")
        # The original text is preserved, just no longer executable.
        assert name_cell[1:] == f"{trigger}cmd|' /c calc'!A0"

    def test_neutralizes_policy_names_paths_and_errors(self):
        root = org_map()["root"]
        root["name"] = "=Root"
        root["path"] = "=Root"
        root["accountsError"] = "=BOOM()"
        root["policies"] = [
            {"id": "p", "name": "=Evil", "type": "SERVICE_CONTROL_POLICY"}
        ]
        row = rows_for(org_map(root=root))[1]
        assert row[1].startswith("'")  # OU Path
        assert row[2].startswith("'")  # Name
        assert row[6].startswith("'")  # SCP names
        assert row[9].startswith("'")  # Error

    def test_leaves_ordinary_values_untouched(self):
        rows = rows_for(org_map())
        assert rows[1][2] == "Root"
        assert rows[1][6] == "FullAWSAccess"

    def test_guards_the_checks_csv_error_column_too(self):
        """Check error text is AWS-provided and lands in a cell as well."""
        content = generate_csv(
            [
                {
                    "check": "Example",
                    "description": "d",
                    "status": "error",
                    "required": True,
                    "weight": 1,
                    "loe": 1,
                    "remediationLink": "https://example.com",
                    "error": "=cmd|' /c calc'!A0",
                }
            ]
        )
        assert list(csv.reader(io.StringIO(content)))[1][7].startswith("'")


class TestPartialMapIsFlagged:
    """A reader who opens only the CSV must not see an incomplete map as whole."""

    def test_policy_lookup_errors_reach_the_root_error_cell(self):
        data = org_map(
            policyLookupErrors=[
                {"scope": "INSPECTOR_POLICY", "error": "TooManyRequestsException"}
            ]
        )
        root_error = rows_for(data)[1][9]
        assert "may be incomplete" in root_error
        assert "INSPECTOR_POLICY" in root_error
        assert "TooManyRequestsException" in root_error

    def test_depth_truncation_is_reported_only_on_the_truncated_node(self):
        """A localizable warning belongs to its own row, and only there.

        The truncated OU's own Error cell says where the walk stopped, so
        repeating it on the root row would report the same condition twice.
        """
        root = org_map()["root"]
        cut_off = root["children"][0]
        cut_off["childrenTruncated"] = "Walk stopped at the limit"
        data = org_map(
            root=root,
            depthTruncated=True,
            depthTruncatedPaths=[cut_off["path"]],
        )
        rows = rows_for(data)

        assert rows[1][9] == ""
        truncated = [row for row in rows[1:] if row[9]]
        assert [row[3] for row in truncated] == [cut_off["id"]]
        assert "Walk stopped at the limit" in truncated[0][9]

    def test_map_warnings_do_not_displace_a_node_note(self):
        root = org_map()["root"]
        root["accountsError"] = "AccessDenied"
        data = org_map(
            root=root,
            policyLookupErrors=[{"scope": "TAG_POLICY", "error": "Denied"}],
        )
        root_error = rows_for(data)[1][9]
        assert "TAG_POLICY" in root_error
        assert "AccessDenied" in root_error

    def test_clean_map_has_no_error_text(self):
        assert rows_for(org_map())[1][9] == ""
