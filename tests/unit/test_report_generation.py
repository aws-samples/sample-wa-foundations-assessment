"""Unit tests for src/report/ — FR-10: Report Generation."""

import csv
import io
import json
from datetime import datetime, timezone

import pytest

from scripts.generate_sample_report import _verify_outputs
from src.checks.maturity import calculate_maturity_level
from src.checks.org_map import flatten_rows
from src.report.csv_export import generate_csv, generate_org_map_csv
from src.report.html_report import (
    ASSETS_DIR,
    CAPABILITY_AXES,
    _calculate_axis_scores,
    generate_html,
)


def _make_org_map():
    """An organization map that satisfies the sample verifier (FR-12).

    The verifier requires the sample to be illustrative: populated OUs and
    accounts, at least one nested OU, a guardrail policy type that is not
    enabled, and both an OU-level and an account-level policy attachment.
    """
    return {
        "organizationId": "o-example12345",
        "managementAccountId": "111111111111",
        "enabledPolicyTypes": ["SERVICE_CONTROL_POLICY"],
        "policyTypesNotEnabled": ["RESOURCE_CONTROL_POLICY"],
        "policyLookupErrors": [],
        "totals": {"organizationalUnits": 2, "accounts": 2, "maxDepth": 2},
        "root": {
            "id": "r-root",
            "name": "Root",
            "type": "ROOT",
            "path": "Root",
            "depth": 0,
            "policies": [
                {
                    "id": "p-FullAWSAccess",
                    "name": "FullAWSAccess",
                    "type": "SERVICE_CONTROL_POLICY",
                    "awsManaged": True,
                }
            ],
            "accounts": [
                {
                    "id": "111111111111",
                    "name": "Management",
                    "status": "ACTIVE",
                    "policies": [],
                }
            ],
            "children": [
                {
                    "id": "ou-wl",
                    "name": "Workloads",
                    "type": "ORGANIZATIONAL_UNIT",
                    "path": "Root/Workloads",
                    "depth": 1,
                    "policies": [],
                    "accounts": [],
                    "children": [
                        {
                            "id": "ou-prod",
                            "name": "Production",
                            "type": "ORGANIZATIONAL_UNIT",
                            "path": "Root/Workloads/Production",
                            "depth": 2,
                            "policies": [
                                {
                                    "id": "p-example-tags",
                                    "name": "RequireCostCenterTag",
                                    "type": "TAG_POLICY",
                                    "awsManaged": False,
                                }
                            ],
                            "accounts": [
                                {
                                    "id": "444444444444",
                                    "name": "Workload-Prod",
                                    "status": "ACTIVE",
                                    "policies": [
                                        {
                                            "id": "p-example-denyroot",
                                            "name": "DenyRootUserActions",
                                            "type": "SERVICE_CONTROL_POLICY",
                                            "awsManaged": False,
                                        }
                                    ],
                                }
                            ],
                            "children": [],
                        }
                    ],
                }
            ],
        },
    }


def _write_org_map_artifacts(tmp_path, org_map):
    """Write the FR-12 organization map CSV the sample verifier expects."""
    (tmp_path / "wafa-org-map.csv").write_text(
        generate_org_map_csv(org_map, flatten_rows(org_map)), encoding="utf-8"
    )


def _make_checks():
    """Create a sample set of checks for testing."""
    return [
        {
            "check": "AWS Organization exists",
            "description": "An AWS Organization should exist.",
            "status": "complete",
            "required": True,
            "weight": 6,
            "loe": 1,
            "remediationLink": "https://docs.aws.amazon.com/example",
        },
        {
            "check": "Service Control Policies enabled",
            "description": "SCPs should be enabled.",
            "status": "incomplete",
            "required": True,
            "weight": 6,
            "loe": 1,
            "remediationLink": "https://docs.aws.amazon.com/scp",
        },
        {
            "check": "Control Tower deployed",
            "description": "Control Tower should be deployed.",
            "status": "error",
            "required": True,
            "weight": 6,
            "loe": 6,
            "remediationLink": "https://docs.aws.amazon.com/ct",
            "error": "AccessDenied",
        },
    ]


def _make_maturity():
    return {
        "level": 2,
        "name": "Established",
        "description": "AWS Organizations in place with OU structure.",
        "next_level": 3,
        "next_level_progress": 45.5,
        "next_level_checks_needed": ["SCP enabled", "Control Tower deployed"],
    }


# ============================================================================
# HTML Report
# ============================================================================


def _report_data(html):
    """Extract the JSON payload the Cloudscape report UI renders."""
    marker = '<script type="application/json" id="wafa-report-data">'
    payload = html.split(marker, 1)[1].split("</script>", 1)[0]
    return json.loads(payload)


class TestGenerateHtml:
    def test_html_is_cloudscape_shell(self):
        """HTML report inlines the Cloudscape bundle and mounts the UI."""
        html = generate_html(_make_checks(), _make_maturity())

        assert html.startswith("<!DOCTYPE html>")
        assert "<title>WA-Foundations Report</title>" in html
        assert '<div id="wafa-report-root"></div>' in html
        assert html.index('id="wafa-report-data"') < html.index("<script>\n")
        # The prebuilt bundle contains the Cloudscape runtime and styles.
        assert "awsui" in html
        assert "awsui-dark-mode" in html
        assert "Well-Architected Foundations Assessment Report" in html

    def test_html_inlines_prebuilt_assets_unchanged(self):
        """The bundle is inlined byte-for-byte and needs no network access."""
        html = generate_html(_make_checks(), _make_maturity())
        for name in ("report-ui.js", "report-ui.css"):
            asset = (ASSETS_DIR / name).read_text(encoding="utf-8")
            assert asset.rstrip("\n") in html
            assert "</script" not in asset.lower()
            assert "</style" not in asset.lower()
            assert all(line == line.rstrip() for line in asset.splitlines())
        assert '<script src="' not in html
        assert '<link rel="stylesheet"' not in html
        assert "cdn.jsdelivr.net" not in html
        assert all(line == line.rstrip() for line in html.splitlines())

    def test_html_has_noscript_fallback(self):
        """Readers without JavaScript are pointed at the CSV and JSON output."""
        html = generate_html(_make_checks(), _make_maturity())
        noscript = html.split("<noscript>", 1)[1].split("</noscript>", 1)[0]
        assert "requires JavaScript" in noscript
        assert "CSV and JSON" in noscript

    def test_report_data_contains_assessment_results(self):
        """The embedded payload carries checks, maturity, and totals."""
        checks = _make_checks()
        maturity = _make_maturity()
        data = _report_data(generate_html(checks, maturity))

        assert data["checks"] == checks
        assert data["maturity"] == maturity
        assert data["delegated_administrators"] == []
        assert data["delegated_administrators_error"] is None
        assert data["account_info"] == {}
        assert data["summary"] == {
            "total": 3,
            "complete": 1,
            "incomplete": 1,
            "errors": 1,
            "pct": 33,
        }

    def test_report_data_includes_structured_scoring_model(self):
        """The UI renders the scoring model returned by the scoring module."""
        checks = _make_checks()
        maturity = calculate_maturity_level(checks)
        data = _report_data(generate_html(checks, maturity))

        model = data["maturity"]["scoring_model"]
        assert len(model["levels"]) == 5
        assert (
            "highest maturity level whose required criteria are all complete"
            in model["rule"]
        )
        assert (
            "Check weights prioritize recommended next steps" in (model["weight_note"])
        )
        assert [level["state"] for level in model["levels"]].count("current") == 1

    def test_report_data_includes_capability_axes(self):
        """Every capability axis is sent to the chart, including gaps."""
        data = _report_data(generate_html(_make_checks(), _make_maturity()))
        axes = {axis["name"]: axis for axis in data["axis_scores"]}

        assert list(axes) == list(CAPABILITY_AXES)
        assert axes["Networking & Connectivity"] == {
            "name": "Networking & Connectivity",
            "score": 0,
            "check_count": 0,
            "assessed_count": 0,
        }
        assert axes["Multi-Account Environment"]["check_count"] == len(
            CAPABILITY_AXES["Multi-Account Environment"]
        )

    def test_report_data_includes_account_and_delegated_admins(self):
        """Account context and delegated administrators reach the UI."""
        admins = [
            {
                "accountId": "222222222222",
                "accountName": "Security",
                "services": ["guardduty.amazonaws.com"],
            }
        ]
        account_info = {
            "account_id": "222222222222",
            "account_type": "member",
            "is_management_account": False,
        }
        data = _report_data(
            generate_html(
                _make_checks(),
                _make_maturity(),
                delegated_admins=admins,
                account_info=account_info,
            )
        )

        assert data["delegated_administrators"] == admins
        assert data["account_info"] == account_info

    def test_report_data_carries_the_map_summary_not_the_tree(self):
        """The UI reads rows plus map-level fields, so the tree is not embedded.

        Embedding both roughly doubles the inline JSON. The complete tree stays
        in ``wafa-raw.json``.
        """
        org_map = _make_org_map()
        org_map["depthTruncated"] = True
        org_map["depthTruncatedPaths"] = ["Root/Workloads/Production"]
        org_map["nodeListingErrors"] = True
        org_map["nodeListingErrorPaths"] = ["Root/Workloads"]
        data = _report_data(
            generate_html(
                _make_checks(),
                _make_maturity(),
                org_map=org_map,
                org_map_rows=flatten_rows(org_map),
            )
        )

        assert "organization_map" not in data
        assert data["organization_map_summary"] == {
            "organizationId": "o-example12345",
            "totals": org_map["totals"],
            "policyLookupErrors": [],
            "policyTypesPendingWarnings": [],
            "depthTruncatedPaths": ["Root/Workloads/Production"],
            "nodeListingErrorPaths": ["Root/Workloads"],
        }
        # Nothing the UI renders is lost: the rows still cover every node.
        rows = data["organization_map_rows"]
        assert len(rows) == len(flatten_rows(org_map))
        assert rows[0]["key"] == "0:root:r-root"

    def test_report_data_labels_policy_lookup_scopes_and_pending_types(self):
        """Alerts use the same policy-type labels as the badges."""
        org_map = _make_org_map()
        org_map["policyLookupErrors"] = [
            {
                "scope": "TAG_POLICY: RequireCostCenterTag",
                "policyType": "TAG_POLICY",
                "policyName": "RequireCostCenterTag",
                "error": "TooManyRequestsException",
            }
        ]
        org_map["policyTypesPending"] = [
            {"type": "SERVICE_CONTROL_POLICY", "status": "PENDING_ENABLE"}
        ]
        summary = _report_data(
            generate_html(
                _make_checks(),
                _make_maturity(),
                org_map=org_map,
                org_map_rows=flatten_rows(org_map),
            )
        )["organization_map_summary"]

        assert summary["policyLookupErrors"] == [
            {"scope": "Tag: RequireCostCenterTag", "error": "TooManyRequestsException"}
        ]
        assert summary["policyTypesPendingWarnings"] == [
            (
                "SCP is changing state (PENDING_ENABLE), so its attachments are "
                "not mapped; re-run the assessment once it settles"
            )
        ]

    def test_report_data_row_keys_are_unique_without_ids(self):
        """An AWS id defaults to "", so a key built from it alone collides.

        React then renders duplicate or wrong rows once the table is filtered
        or paged.
        """
        org_map = _make_org_map()
        for node in org_map["root"]["children"]:
            node["id"] = ""
        org_map["root"]["children"].append(
            {
                "id": "",
                "name": "Another nameless OU",
                "type": "ORGANIZATIONAL_UNIT",
                "path": "Root/Another nameless OU",
                "depth": 1,
                "policies": [],
                "accounts": [{"id": "", "name": "No id", "status": "", "policies": []}],
                "children": [],
            }
        )
        data = _report_data(
            generate_html(
                _make_checks(),
                _make_maturity(),
                org_map=org_map,
                org_map_rows=flatten_rows(org_map),
            )
        )

        keys = [row["key"] for row in data["organization_map_rows"]]
        assert len(keys) == len(set(keys))

    def test_report_data_has_no_map_summary_without_a_map(self):
        data = _report_data(generate_html(_make_checks(), _make_maturity()))
        assert data["organization_map_summary"] is None
        assert data["organization_map_rows"] == []

    def test_html_escapes_untrusted_check_content(self):
        """Dynamic check content cannot break out of the JSON data element."""
        checks = _make_checks()
        checks[0]["description"] = '</script><script>alert("xss")</script>'
        checks[1]["check"] = "<!-- & ' \u2028"
        html = generate_html(checks, _make_maturity())

        assert "<script>alert" not in html
        assert "</script><script>" not in html
        assert "\\u003c/script\\u003e" in html
        data = _report_data(html)
        assert data["checks"][0]["description"] == checks[0]["description"]
        assert data["checks"][1]["check"] == checks[1]["check"]

    def test_report_data_serializes_non_json_values(self):
        """Non-JSON AWS values are stringified like the raw JSON export."""
        created = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        data = _report_data(
            generate_html(
                _make_checks(),
                _make_maturity(),
                account_info={"account_id": "111111111111", "created": created},
            )
        )
        assert data["account_info"]["created"] == str(created)


class TestCalculateAxisScores:
    def test_all_complete(self):
        """All checks complete → 100% per axis."""
        checks = [
            {"check": "AWS Organization exists", "status": "complete"},
            {"check": "Management account identified", "status": "complete"},
            {"check": "Minimum 4 accounts", "status": "complete"},
            {"check": "Log Archive account exists", "status": "complete"},
            {"check": "Audit account exists", "status": "complete"},
            {"check": "Service Control Policies enabled", "status": "complete"},
            {"check": "Tag Policies enabled", "status": "complete"},
            {"check": "Backup Policies enabled", "status": "complete"},
            {"check": "Resource Control Policies enabled", "status": "complete"},
            {"check": "Security OU exists", "status": "complete"},
            {"check": "Workloads OU exists", "status": "complete"},
            {"check": "Infrastructure OU exists", "status": "complete"},
        ]
        scores = _calculate_axis_scores(checks)
        assert scores["Multi-Account Environment"] == 100

    def test_networking_axis_is_explicit_gap(self):
        """Networking remains visible as a zero-score Phase 1 gap."""
        checks = [{"check": "Some check", "status": "complete"}]
        scores = _calculate_axis_scores(checks)
        assert scores["Networking & Connectivity"] == 0
        assert len(scores) == 7

    def test_unassessed_checks_are_not_gaps(self):
        """Checks skipped in limited mode do not lower an axis score."""
        checks = [{"check": "AWS Organization exists", "status": "complete"}]
        scores = _calculate_axis_scores(checks)
        assert scores["Multi-Account Environment"] == 100
        assert scores["Cloud Financial Management"] == 0

    def test_report_data_counts_assessed_checks(self):
        from src.report.html_report import _build_report_data

        checks = [
            {"check": "AWS Organization exists", "status": "complete"},
            {"check": "No IAM users in account", "status": "incomplete"},
        ]
        data = _build_report_data(checks, {"level": 1}, [], {})
        axes = {axis["name"]: axis for axis in data["axis_scores"]}
        multi = axes["Multi-Account Environment"]
        assert multi["assessed_count"] == 1
        assert multi["check_count"] == len(CAPABILITY_AXES["Multi-Account Environment"])
        assert data["delegated_administrators_error"] is None

    def test_partial_scores(self):
        """Partial completion shows proportional score."""
        checks = [
            {"check": "CloudTrail organization service enabled", "status": "complete"},
            {"check": "CloudTrail trail exists", "status": "incomplete"},
            {"check": "CloudTrail organization trail", "status": "incomplete"},
        ]
        scores = _calculate_axis_scores(checks)
        # 1 out of 3 for Central Observability
        assert scores["Central Observability"] == 33


# ============================================================================
# CSV Export
# ============================================================================


class TestGenerateCsv:
    def test_csv_has_header(self):
        """CSV starts with header row."""
        checks = _make_checks()
        csv_content = generate_csv(checks)

        lines = csv_content.strip().split("\n")
        assert "Check Name" in lines[0]
        assert "Status" in lines[0]
        assert "Required" in lines[0]

    def test_csv_has_all_rows(self):
        """CSV has one row per check + header."""
        checks = _make_checks()
        csv_content = generate_csv(checks)

        lines = csv_content.strip().split("\n")
        assert len(lines) == 4  # 1 header + 3 data rows

    def test_csv_contains_check_data(self):
        """CSV contains actual check data."""
        checks = _make_checks()
        csv_content = generate_csv(checks)

        assert "AWS Organization exists" in csv_content
        assert "complete" in csv_content
        assert "incomplete" in csv_content
        assert "error" in csv_content
        assert "https://docs.aws.amazon.com/example" in csv_content

    def test_csv_error_field(self):
        """CSV includes error message for error checks."""
        checks = _make_checks()
        csv_content = generate_csv(checks)

        assert "AccessDenied" in csv_content

    def test_sample_verifier_rejects_non_name_csv_drift(self, tmp_path):
        """Sample verification compares every CSV field with raw JSON."""
        checks = _make_checks()
        maturity = calculate_maturity_level(checks)
        org_map = _make_org_map()
        raw = {
            "checks": checks,
            "maturity": maturity,
            "account_info": {},
            "delegated_administrators": [],
            "delegated_administrators_error": None,
            "organization_map": org_map,
            "organization_map_error": None,
        }

        (tmp_path / "wafa-report.html").write_text(
            generate_html(
                checks, maturity, org_map=org_map, org_map_rows=flatten_rows(org_map)
            ),
            encoding="utf-8",
        )
        (tmp_path / "wafa-raw.json").write_text(
            json.dumps(raw, indent=2) + "\n", encoding="utf-8"
        )
        csv_path = tmp_path / "wafa-checks.csv"
        csv_path.write_text(generate_csv(checks), encoding="utf-8")
        _write_org_map_artifacts(tmp_path, org_map)
        _verify_outputs(tmp_path)

        rows = list(csv.reader(io.StringIO(csv_path.read_text(encoding="utf-8"))))
        rows[1][2] = "incomplete"
        corrupted_csv = io.StringIO()
        csv.writer(corrupted_csv).writerows(rows)
        csv_path.write_text(corrupted_csv.getvalue(), encoding="utf-8")

        with pytest.raises(
            SystemExit, match="wafa-checks.csv data differs from wafa-raw.json checks"
        ):
            _verify_outputs(tmp_path)

    def test_sample_verifier_rejects_html_json_metadata_drift(self, tmp_path):
        """Account info and delegated admins must match between HTML and JSON."""
        checks = _make_checks()
        maturity = calculate_maturity_level(checks)
        account_info = {"account_id": "111111111111", "account_type": "management"}
        admins = [{"accountId": "333333333333", "accountName": "Audit", "services": []}]
        org_map = _make_org_map()
        raw = {
            "account_info": account_info,
            "maturity": maturity,
            "checks": checks,
            "delegated_administrators": admins,
            "delegated_administrators_error": None,
            "organization_map": org_map,
            "organization_map_error": None,
        }
        (tmp_path / "wafa-checks.csv").write_text(
            generate_csv(checks), encoding="utf-8"
        )
        raw_path = tmp_path / "wafa-raw.json"
        raw_path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        (tmp_path / "wafa-report.html").write_text(
            generate_html(
                checks,
                maturity,
                admins,
                account_info,
                org_map=org_map,
                org_map_rows=flatten_rows(org_map),
            ),
            encoding="utf-8",
        )
        _write_org_map_artifacts(tmp_path, org_map)
        _verify_outputs(tmp_path)

        for key, value in (
            ("account_info", {"account_id": "999999999999"}),
            ("delegated_administrators", []),
            ("delegated_administrators_error", "Denied"),
            ("organization_map_error", "Denied"),
        ):
            raw_path.write_text(
                json.dumps({**raw, key: value}, indent=2) + "\n", encoding="utf-8"
            )
            with pytest.raises(SystemExit, match=f"HTML report data {key} differs"):
                _verify_outputs(tmp_path)
