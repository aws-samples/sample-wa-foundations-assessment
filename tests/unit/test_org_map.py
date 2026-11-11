"""Unit tests for src/checks/org_map.py — FR-12: Organization and OU account map."""

from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError

from src.checks.org_map import MAX_OU_DEPTH, flatten_rows, get_organization_map


def client_error(code, operation="ListPolicies"):
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class _Paginator:
    """Stands in for a botocore paginator around a single callable."""

    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kwargs):
        return self._pages(**kwargs)


class FakeOrganizations:
    """In-memory AWS Organizations stand-in.

    Uses a real object rather than a MagicMock because the policy lookups run
    on a thread pool, and the tests assert on recorded calls.
    """

    def __init__(
        self,
        ous=None,
        accounts=None,
        policies_by_type=None,
        targets_by_policy=None,
        policy_types=(
            ("SERVICE_CONTROL_POLICY", "ENABLED"),
            ("RESOURCE_CONTROL_POLICY", "ENABLED"),
        ),
        roots=None,
        ou_errors=None,
        account_errors=None,
        list_policies_errors=None,
        targets_errors=None,
        page_size=None,
    ):
        self.ous = ous or {}
        self.accounts = accounts or {}
        self.policies_by_type = policies_by_type or {}
        self.targets_by_policy = targets_by_policy or {}
        self.policy_types = policy_types
        self.roots = roots
        self.ou_errors = ou_errors or {}
        self.account_errors = account_errors or {}
        self.list_policies_errors = list_policies_errors or {}
        self.targets_errors = targets_errors or {}
        # Organizations pages at roughly 20 items. page_size=1 forces every
        # list to span multiple pages, so code that reads only the first page
        # is caught instead of passing against a single-page fake.
        self.page_size = page_size
        # list.append is atomic, so these stay correct under the thread pool.
        self.list_policies_calls = []
        self.targets_calls = []

    def describe_organization(self):
        return {
            "Organization": {
                "Id": "o-example12345",
                "MasterAccountId": "111111111111",
            }
        }

    def list_roots(self):
        if self.roots is not None:
            return {"Roots": self.roots}
        return {
            "Roots": [
                {
                    "Id": "r-root",
                    "Name": "Root",
                    "PolicyTypes": [
                        {"Type": name, "Status": status}
                        for name, status in self.policy_types
                    ],
                }
            ]
        }

    def _paged(self, key, items):
        """Yield items as one page, or as several when page_size is set."""
        items = items or []
        if not self.page_size:
            yield {key: items}
            return
        for start in range(0, max(len(items), 1), self.page_size):
            yield {key: items[start : start + self.page_size]}

    def _ou_pages(self, ParentId):
        if ParentId in self.ou_errors:
            raise self.ou_errors[ParentId]
        yield from self._paged("OrganizationalUnits", self.ous.get(ParentId, []))

    def _account_pages(self, ParentId):
        if ParentId in self.account_errors:
            raise self.account_errors[ParentId]
        yield from self._paged("Accounts", self.accounts.get(ParentId, []))

    def _list_policies_pages(self, Filter):
        self.list_policies_calls.append(Filter)
        if Filter in self.list_policies_errors:
            raise self.list_policies_errors[Filter]
        yield from self._paged("Policies", self.policies_by_type.get(Filter, []))

    def _targets_pages(self, PolicyId):
        self.targets_calls.append(PolicyId)
        if PolicyId in self.targets_errors:
            raise self.targets_errors[PolicyId]
        yield from self._paged("Targets", self.targets_by_policy.get(PolicyId, []))

    def get_paginator(self, name):
        pages = {
            "list_organizational_units_for_parent": self._ou_pages,
            "list_accounts_for_parent": self._account_pages,
            "list_policies": self._list_policies_pages,
            "list_targets_for_policy": self._targets_pages,
        }[name]
        return _Paginator(pages)


def policy(policy_id, name, policy_type="SERVICE_CONTROL_POLICY", aws_managed=False):
    return {
        "Id": policy_id,
        "Name": name,
        "Type": policy_type,
        "AwsManaged": aws_managed,
    }


def target(target_id, target_type="ORGANIZATIONAL_UNIT"):
    return {"TargetId": target_id, "Type": target_type, "Name": target_id}


def sample_org(**kwargs):
    """A two-level organization with policies attached at several levels."""
    defaults = {
        "ous": {
            "r-root": [
                {"Id": "ou-sec", "Name": "Security"},
                {"Id": "ou-wl", "Name": "Workloads"},
            ],
            "ou-wl": [{"Id": "ou-prod", "Name": "Prod"}],
        },
        "accounts": {
            "r-root": [
                {"Id": "111111111111", "Name": "Management", "Status": "ACTIVE"}
            ],
            "ou-sec": [
                {"Id": "222222222222", "Name": "Log Archive", "Status": "ACTIVE"},
                {"Id": "333333333333", "Name": "Audit", "Status": "SUSPENDED"},
            ],
            "ou-prod": [
                {"Id": "444444444444", "Name": "Workload-Prod", "Status": "ACTIVE"}
            ],
        },
        "policies_by_type": {
            "SERVICE_CONTROL_POLICY": [
                policy("p-full", "FullAWSAccess", aws_managed=True),
                policy("p-deny", "DenyRootUser"),
            ],
            "RESOURCE_CONTROL_POLICY": [
                policy("p-perim", "DataPerimeter", "RESOURCE_CONTROL_POLICY")
            ],
        },
        "targets_by_policy": {
            "p-full": [target("r-root", "ROOT")],
            "p-deny": [target("ou-sec"), target("444444444444", "ACCOUNT")],
            "p-perim": [target("ou-prod")],
        },
    }
    defaults.update(kwargs)
    return FakeOrganizations(**defaults)


def run_map(fake):
    with patch("src.checks.org_map.boto3.client", return_value=fake):
        return get_organization_map()


def find_node(node, node_id):
    if node["id"] == node_id:
        return node
    for child in node["children"]:
        found = find_node(child, node_id)
        if found:
            return found
    return None


def find_account(node, account_id):
    for account in node["accounts"]:
        if account["id"] == account_id:
            return account
    for child in node["children"]:
        found = find_account(child, account_id)
        if found:
            return found
    return None


class TestOrganizationMapStructure:
    def test_maps_hierarchy_and_account_placement(self):
        data, error = run_map(sample_org())

        assert error is None
        assert data["organizationId"] == "o-example12345"
        assert data["totals"] == {
            "organizationalUnits": 3,
            "accounts": 4,
            "maxDepth": 2,
        }

        root = data["root"]
        assert root["type"] == "ROOT"
        assert root["path"] == "Root"
        assert [a["id"] for a in root["accounts"]] == ["111111111111"]

        security = find_node(root, "ou-sec")
        assert security["path"] == "Root/Security"
        assert security["depth"] == 1
        # Accounts are sorted by name, so Audit precedes Log Archive.
        assert [a["name"] for a in security["accounts"]] == ["Audit", "Log Archive"]

        prod = find_node(root, "ou-prod")
        assert prod["path"] == "Root/Workloads/Prod"
        assert prod["depth"] == 2

    def test_omits_account_email(self):
        """Email is personal data and is not needed to place an account."""
        fake = sample_org(
            accounts={
                "r-root": [
                    {
                        "Id": "111111111111",
                        "Name": "Management",
                        "Status": "ACTIVE",
                        "Email": "owner@example.com",
                    }
                ]
            }
        )
        data, _ = run_map(fake)
        account = data["root"]["accounts"][0]
        assert set(account) == {"id", "name", "status", "policies"}
        assert "example.com" not in str(data)

    def test_preserves_suspended_account_status(self):
        data, _ = run_map(sample_org())
        assert find_account(data["root"], "333333333333")["status"] == "SUSPENDED"

    def test_defaults_status_to_unknown(self):
        fake = sample_org(
            accounts={"r-root": [{"Id": "111111111111", "Name": "Management"}]}
        )
        data, _ = run_map(fake)
        assert data["root"]["accounts"][0]["status"] == "UNKNOWN"

    def test_marks_hierarchy_deeper_than_the_depth_guard(self):
        """Nesting past the guard is reported, not silently dropped."""
        ous = {"r-root": [{"Id": "ou-1", "Name": "L1"}]}
        for level in range(1, MAX_OU_DEPTH + 2):
            ous[f"ou-{level}"] = [{"Id": f"ou-{level + 1}", "Name": f"L{level + 1}"}]
        data, error = run_map(FakeOrganizations(ous=ous, policy_types=()))

        assert error is None
        assert data["depthTruncated"] is True
        assert data["totals"]["maxDepth"] == MAX_OU_DEPTH

        # The node where the walk stopped says so, so the gap is locatable
        # rather than only flagged once for the whole organization.
        deepest = next(
            row for row in flatten_rows(data) if row["depth"] == MAX_OU_DEPTH
        )
        assert "nesting limit" in deepest["note"]
        assert "not listed" in deepest["note"]
        # The map-level field names those nodes, so a summary can locate the
        # gap without restating the warning each node already carries.
        assert data["depthTruncatedPaths"] == [deepest["path"]]

    def test_hierarchy_exactly_at_the_depth_guard_is_not_truncated(self):
        """An OU at the nesting limit has no children, so nothing was missed."""
        ous = {"r-root": [{"Id": "ou-1", "Name": "L1"}]}
        for level in range(1, MAX_OU_DEPTH):
            ous[f"ou-{level}"] = [{"Id": f"ou-{level + 1}", "Name": f"L{level + 1}"}]
        data, error = run_map(FakeOrganizations(ous=ous, policy_types=()))

        assert error is None
        assert data["totals"]["maxDepth"] == MAX_OU_DEPTH
        assert "depthTruncated" not in data

    def test_no_truncation_flag_within_the_depth_guard(self):
        data, _ = run_map(sample_org())
        assert "depthTruncated" not in data
        assert "depthTruncatedPaths" not in data


class TestConcurrentWalk:
    """The walk runs a level at a time through a bounded pool.

    Concurrency must not change the tree: the results are consumed in level
    order, so ordering, totals, and depth stay deterministic.
    """

    def test_wide_level_keeps_sorted_order_and_exact_totals(self):
        names = ["Zeta", "alpha", "Mu", "beta", "Omega"] * 6
        ous = {
            "r-root": [
                {"Id": f"ou-{index:02d}", "Name": f"{name}-{index:02d}"}
                for index, name in enumerate(names)
            ]
        }
        accounts = {
            f"ou-{index:02d}": [{"Id": f"{index:012d}", "Name": f"Account-{index:02d}"}]
            for index in range(len(names))
        }
        data, error = run_map(
            FakeOrganizations(ous=ous, accounts=accounts, policy_types=())
        )

        assert error is None
        assert data["totals"] == {
            "organizationalUnits": len(names),
            "accounts": len(names),
            "maxDepth": 1,
        }
        expected = sorted(
            (ou["Name"] for ou in ous["r-root"]), key=lambda name: name.lower()
        )
        assert [child["name"] for child in data["root"]["children"]] == expected

    def test_a_failed_node_does_not_stop_the_rest_of_its_level(self):
        ous = {
            "r-root": [
                {"Id": "ou-a", "Name": "A"},
                {"Id": "ou-b", "Name": "B"},
                {"Id": "ou-c", "Name": "C"},
            ],
            "ou-b": [{"Id": "ou-b1", "Name": "B1"}],
            "ou-c": [{"Id": "ou-c1", "Name": "C1"}],
        }
        fake = FakeOrganizations(
            ous=ous,
            account_errors={"ou-a": client_error("AccessDeniedException")},
            ou_errors={"ou-b": client_error("ThrottlingException")},
            policy_types=(),
        )
        data, error = run_map(fake)

        assert error is None
        root = data["root"]
        assert "AccessDeniedException" in find_node(root, "ou-a")["accountsError"]
        assert "ThrottlingException" in find_node(root, "ou-b")["childrenError"]
        # The sibling after both failures was still walked into.
        assert find_node(root, "ou-c1") is not None
        assert data["totals"]["organizationalUnits"] == 4


class TestPagination:
    """Organizations pages at ~20 items; reading one page silently drops data."""

    def test_collects_every_page_of_every_list(self):
        fake = sample_org(page_size=1)
        data, error = run_map(fake)

        assert error is None
        # All OUs across multiple pages of ListOrganizationalUnitsForParent.
        assert data["totals"]["organizationalUnits"] == 3
        assert [ou["name"] for ou in data["root"]["children"]] == [
            "Security",
            "Workloads",
        ]
        # All accounts across multiple pages of ListAccountsForParent.
        assert data["totals"]["accounts"] == 4
        assert [a["name"] for a in find_node(data["root"], "ou-sec")["accounts"]] == [
            "Audit",
            "Log Archive",
        ]
        # All policies across multiple pages of ListPolicies, and all targets
        # across multiple pages of ListTargetsForPolicy.
        assert [p["name"] for p in data["root"]["policies"]] == ["FullAWSAccess"]
        assert [p["name"] for p in find_node(data["root"], "ou-sec")["policies"]] == [
            "DenyRootUser"
        ]
        assert [
            p["name"] for p in find_account(data["root"], "444444444444")["policies"]
        ] == ["DenyRootUser"]

    def test_paged_and_unpaged_results_agree(self):
        single, _ = run_map(sample_org())
        paged, _ = run_map(sample_org(page_size=1))
        assert single == paged


class TestPathEscaping:
    def test_escapes_a_slash_inside_an_ou_name(self):
        """An OU named "Prod/EU" must not collide with "EU" inside "Prod"."""
        fake = sample_org(
            ous={"r-root": [{"Id": "ou-x", "Name": "Prod/EU"}]},
            accounts={},
        )
        data, _ = run_map(fake)
        assert find_node(data["root"], "ou-x")["path"] == "Root/Prod\\/EU"

    def test_nested_ous_keep_their_distinct_path(self):
        fake = sample_org(
            ous={
                "r-root": [{"Id": "ou-p", "Name": "Prod"}],
                "ou-p": [{"Id": "ou-e", "Name": "EU"}],
            },
            accounts={},
        )
        data, _ = run_map(fake)
        # Distinct from the escaped "Root/Prod\/EU" above.
        assert find_node(data["root"], "ou-e")["path"] == "Root/Prod/EU"


class TestPolicyAttachment:
    def test_attaches_policies_to_root_ou_and_account(self):
        data, error = run_map(sample_org())

        assert error is None
        assert data["policyLookupErrors"] == []
        assert [p["name"] for p in data["root"]["policies"]] == ["FullAWSAccess"]
        assert [p["name"] for p in find_node(data["root"], "ou-sec")["policies"]] == [
            "DenyRootUser"
        ]
        assert [p["type"] for p in find_node(data["root"], "ou-prod")["policies"]] == [
            "RESOURCE_CONTROL_POLICY"
        ]

    def test_account_level_attachments_need_no_opt_in(self):
        """The inverted lookup returns account targets at no extra cost."""
        data, _ = run_map(sample_org())
        account = find_account(data["root"], "444444444444")
        assert [p["name"] for p in account["policies"]] == ["DenyRootUser"]
        # An account with nothing attached still has the key, as an empty list.
        assert find_account(data["root"], "111111111111")["policies"] == []

    def test_preserves_aws_managed_flag(self):
        data, _ = run_map(sample_org())
        assert data["root"]["policies"][0]["awsManaged"] is True

    def test_call_volume_scales_with_policies_not_org_size(self):
        """FR-12.3: cost is types + policies, not targets x types."""
        fake = sample_org()
        run_map(fake)

        # 2 enabled types, 3 policies -> 5 calls, including account attachments.
        assert sorted(fake.list_policies_calls) == [
            "RESOURCE_CONTROL_POLICY",
            "SERVICE_CONTROL_POLICY",
        ]
        assert sorted(fake.targets_calls) == ["p-deny", "p-full", "p-perim"]
        total = len(fake.list_policies_calls) + len(fake.targets_calls)
        # Per-target lookups would have cost 8 node-target calls alone, and 16
        # once accounts are included.
        assert total == 5

    def test_queries_only_enabled_policy_types(self):
        fake = sample_org(
            policy_types=(
                ("SERVICE_CONTROL_POLICY", "ENABLED"),
                ("TAG_POLICY", "PENDING_DISABLE"),
                ("BACKUP_POLICY", "ENABLED"),
            )
        )
        data, _ = run_map(fake)

        assert data["enabledPolicyTypes"] == [
            "BACKUP_POLICY",
            "SERVICE_CONTROL_POLICY",
        ]
        assert "TAG_POLICY" not in fake.list_policies_calls

    def test_reports_guardrail_policy_types_that_are_not_enabled(self):
        fake = sample_org(policy_types=(("TAG_POLICY", "ENABLED"),))
        data, _ = run_map(fake)
        assert data["policyTypesNotEnabled"] == [
            "SERVICE_CONTROL_POLICY",
            "RESOURCE_CONTROL_POLICY",
        ]

    def test_skips_policy_lookups_when_no_type_is_enabled(self):
        fake = sample_org(policy_types=())
        data, error = run_map(fake)
        assert error is None
        assert fake.list_policies_calls == []
        assert fake.targets_calls == []
        assert data["root"]["policies"] == []

    def test_orders_policies_deterministically(self):
        fake = sample_org(
            policies_by_type={
                "SERVICE_CONTROL_POLICY": [
                    policy("p-b", "bravo"),
                    policy("p-a", "alpha"),
                ],
                "RESOURCE_CONTROL_POLICY": [
                    policy("p-c", "charlie", "RESOURCE_CONTROL_POLICY")
                ],
            },
            targets_by_policy={
                "p-a": [target("r-root", "ROOT")],
                "p-b": [target("r-root", "ROOT")],
                "p-c": [target("r-root", "ROOT")],
            },
        )
        data, _ = run_map(fake)
        assert [(p["type"], p["name"]) for p in data["root"]["policies"]] == [
            ("RESOURCE_CONTROL_POLICY", "charlie"),
            ("SERVICE_CONTROL_POLICY", "alpha"),
            ("SERVICE_CONTROL_POLICY", "bravo"),
        ]

    def test_ignores_targets_outside_the_walked_tree(self):
        fake = sample_org(
            targets_by_policy={
                "p-full": [target("ou-unknown"), target("r-root", "ROOT")]
            }
        )
        data, error = run_map(fake)
        assert error is None
        assert [p["name"] for p in data["root"]["policies"]] == ["FullAWSAccess"]


class TestPolicyLookupFailures:
    @pytest.mark.parametrize(
        "code", ["PolicyTypeNotEnabledException", "UnsupportedAPIEndpointException"]
    )
    def test_unavailable_policy_type_contradiction_is_reported(self, code):
        """Only types the root reports as ENABLED are queried.

        So this code is a disagreement between two AWS answers, not a
        confirmed absence. Skipping it silently would show the type as enabled
        with no attachment anywhere, which is indistinguishable from "enabled
        but genuinely unused".
        """
        fake = sample_org(
            list_policies_errors={"SERVICE_CONTROL_POLICY": client_error(code)}
        )
        data, error = run_map(fake)

        assert error is None
        assert [e["scope"] for e in data["policyLookupErrors"]] == [
            "SERVICE_CONTROL_POLICY"
        ]
        reported = data["policyLookupErrors"][0]["error"]
        assert "reports this policy type as ENABLED" in reported
        assert code in reported
        # The type is still listed as enabled, so the two must not disagree
        # silently.
        assert "SERVICE_CONTROL_POLICY" in data["enabledPolicyTypes"]
        # The other type still resolved.
        assert [p["name"] for p in find_node(data["root"], "ou-prod")["policies"]] == [
            "DataPerimeter"
        ]

    def test_policy_type_listing_failure_is_reported(self):
        fake = sample_org(
            list_policies_errors={
                "SERVICE_CONTROL_POLICY": client_error("AccessDeniedException")
            }
        )
        data, error = run_map(fake)

        assert error is None
        assert [e["scope"] for e in data["policyLookupErrors"]] == [
            "SERVICE_CONTROL_POLICY"
        ]
        assert "AccessDeniedException" in data["policyLookupErrors"][0]["error"]
        # Policies of the readable type are still attached.
        assert [p["name"] for p in find_node(data["root"], "ou-prod")["policies"]] == [
            "DataPerimeter"
        ]

    def test_target_listing_failure_is_scoped_to_that_policy(self):
        fake = sample_org(
            targets_errors={"p-deny": client_error("TooManyRequestsException")}
        )
        data, error = run_map(fake)

        assert error is None
        assert [e["scope"] for e in data["policyLookupErrors"]] == [
            "SERVICE_CONTROL_POLICY: DenyRootUser"
        ]
        assert "TooManyRequests" in data["policyLookupErrors"][0]["error"]
        # Other policies still landed.
        assert [p["name"] for p in data["root"]["policies"]] == ["FullAWSAccess"]
        assert find_account(data["root"], "444444444444")["policies"] == []


class TestPartialFailures:
    def test_account_listing_failure_keeps_the_rest_of_the_map(self):
        fake = sample_org(
            account_errors={"ou-sec": client_error("AccessDeniedException")}
        )
        data, error = run_map(fake)

        assert error is None
        security = find_node(data["root"], "ou-sec")
        assert security["accounts"] == []
        assert "AccessDeniedException" in security["accountsError"]
        assert find_node(data["root"], "ou-prod")["accounts"][0]["id"] == (
            "444444444444"
        )

    def test_child_ou_listing_failure_is_recorded_on_the_node(self):
        fake = sample_org(ou_errors={"ou-wl": client_error("ThrottlingException")})
        data, error = run_map(fake)

        assert error is None
        workloads = find_node(data["root"], "ou-wl")
        assert workloads["children"] == []
        assert "ThrottlingException" in workloads["childrenError"]
        assert find_node(data["root"], "ou-prod") is None


class TestClientConfiguration:
    def test_uses_adaptive_retries(self):
        """Organizations throttles hard; standard mode has no rate limiting."""
        fake = sample_org()
        with patch("src.checks.org_map.boto3.client", return_value=fake) as client:
            get_organization_map()

        config = client.call_args.kwargs["config"]
        assert config.retries["mode"] == "adaptive"
        assert config.retries["max_attempts"] >= 10


class TestTopLevelFailures:
    def test_missing_root_reports_an_error(self):
        data, error = run_map(FakeOrganizations(roots=[]))
        assert data is None
        assert "ListRoots" in error

    def test_organization_not_in_use_is_absence_not_error(self):
        fake = sample_org()

        with (
            patch("src.checks.org_map.boto3.client", return_value=fake),
            patch.object(
                fake,
                "describe_organization",
                side_effect=client_error(
                    "AWSOrganizationsNotInUseException", "DescribeOrganization"
                ),
            ),
        ):
            data, error = get_organization_map()

        assert data is None
        assert error is None

    def test_access_denied_reports_an_error(self):
        fake = sample_org()

        with (
            patch("src.checks.org_map.boto3.client", return_value=fake),
            patch.object(
                fake,
                "describe_organization",
                side_effect=client_error(
                    "AccessDeniedException", "DescribeOrganization"
                ),
            ),
        ):
            data, error = get_organization_map()

        assert data is None
        assert "AccessDeniedException" in error

    def test_unexpected_exception_reports_an_error(self):
        with patch("src.checks.org_map.boto3.client", side_effect=RuntimeError("boom")):
            data, error = get_organization_map()

        assert data is None
        assert "boom" in error


class TestFlattenRows:
    def test_orders_parents_before_children_with_depths(self):
        data, _ = run_map(sample_org())
        rows = flatten_rows(data)
        assert [(r["name"], r["kind"], r["depth"]) for r in rows] == [
            ("Root", "root", 0),
            ("Management", "account", 1),
            ("Security", "ou", 1),
            ("Audit", "account", 2),
            ("Log Archive", "account", 2),
            ("Workloads", "ou", 1),
            ("Prod", "ou", 2),
            ("Workload-Prod", "account", 3),
        ]

    def test_account_rows_carry_the_containing_ou_path(self):
        data, _ = run_map(sample_org())
        audit = next(r for r in flatten_rows(data) if r["name"] == "Audit")
        assert audit["path"] == "Root/Security"
        assert audit["status"] == "SUSPENDED"

    def test_can_exclude_accounts(self):
        data, _ = run_map(sample_org())
        rows = flatten_rows(data, include_accounts=False)
        assert {r["kind"] for r in rows} == {"root", "ou"}

    def test_joins_node_errors_into_a_note(self):
        fake = sample_org(
            account_errors={"ou-sec": client_error("AccessDenied")},
            ou_errors={"ou-sec": client_error("Throttled")},
        )
        data, _ = run_map(fake)
        row = next(r for r in flatten_rows(data) if r["id"] == "ou-sec")
        assert "AccessDenied" in row["note"]
        assert "Throttled" in row["note"]

    def test_returns_nothing_without_a_map(self):
        assert flatten_rows(None) == []
        assert flatten_rows({}) == []
