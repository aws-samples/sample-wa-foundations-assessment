"""Organization and OU Account Map (FR-12).

Informational only — no pass/fail scoring. Like ``delegated_admin.py``, this
module intentionally does not use the check contract, so it never contributes
to maturity scoring or the capability axes.

Only runs from the management account. Every call is a read-only Organizations
operation.

The map records, for the organization root and every OU beneath it:

- the OU hierarchy, as a nested tree
- which accounts sit directly in each OU, including suspended ones
- the policies attached *directly* to that root, OU, or account

Policy attachments are collected by enumerating the organization's policies and
asking where each one is attached (``ListPolicies`` then
``ListTargetsForPolicy``), rather than asking every target which policies it
has. Both report the same thing — only *direct* attachments, never ones
inherited from a parent — but the cost differs sharply:

- per-target lookups cost ``targets x enabled policy types``, which grows with
  the size of the organization and throttles on large ones
- enumerating policies costs ``enabled policy types + policies``, which grows
  only with the number of policies

A 500-OU organization with 50 policies needs roughly 59 calls this way instead
of several thousand. Account-level attachments also come back in the same
responses, so they need no extra calls and are always included.

An empty policy list for a level means nothing is attached *there*; that level
may still inherit a policy from an ancestor.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# AWS Organizations allows five levels of OU nesting below the root. The guard
# bounds recursion rather than trusting the service limit.
MAX_OU_DEPTH = 5

# Organizations enforces a low request rate. Adaptive retries add client-side
# rate limiting, which standard mode does not have, so a burst of concurrent
# reads backs off instead of exhausting its attempts and losing data.
_CLIENT_CONFIG = Config(retries={"max_attempts": 10, "mode": "adaptive"})

# Bounded fan-out for the policy lookups.
_MAX_WORKERS = 4

# Always reported, so the map states plainly whether the two guardrail policy
# types are in use rather than staying silent when they are not enabled.
ALWAYS_REPORTED_POLICY_TYPES = ("SERVICE_CONTROL_POLICY", "RESOURCE_CONTROL_POLICY")

# A policy type that is absent here is an observed absence, not a failure to
# determine state: the type is either not enabled in this organization or not
# offered in this partition (for example aws-us-gov or aws-cn).
_POLICY_TYPE_UNAVAILABLE_CODES = frozenset(
    {
        "PolicyTypeNotEnabledException",
        "UnsupportedAPIEndpointException",
    }
)


def _account_entry(account):
    """Summarize an account for the map.

    Deliberately omits the account email address: it is personal data and is
    not needed to show where an account sits in the hierarchy.
    """
    state = account.get("State") or account.get("Status") or ""
    return {
        "id": account.get("Id", ""),
        "name": account.get("Name", ""),
        "status": state.upper() or "UNKNOWN",
        "policies": [],
    }


def _list_child_accounts(client, parent_id):
    """List accounts directly under a root or OU, fully paginated."""
    accounts = []
    paginator = client.get_paginator("list_accounts_for_parent")
    for page in paginator.paginate(ParentId=parent_id):
        accounts.extend(page.get("Accounts", []))
    return accounts


def _list_child_ous(client, parent_id):
    """List OUs directly under a root or OU, fully paginated."""
    ous = []
    paginator = client.get_paginator("list_organizational_units_for_parent")
    for page in paginator.paginate(ParentId=parent_id):
        ous.extend(page.get("OrganizationalUnits", []))
    return ous


def _build_node(client, node_id, name, node_type, path, depth, stats):
    """Build one tree node and recurse into its child OUs.

    Failures are recorded on the node and never raised, so one unreadable OU
    cannot collapse the whole map.
    """
    node = {
        "id": node_id,
        "name": name,
        "type": node_type,
        "path": path,
        "depth": depth,
        "policies": [],
        "accounts": [],
        "children": [],
    }
    stats["max_depth"] = max(stats["max_depth"], depth)

    try:
        accounts = [_account_entry(a) for a in _list_child_accounts(client, node_id)]
        accounts.sort(key=lambda a: (a["name"].lower(), a["id"]))
        node["accounts"] = accounts
        stats["accounts"] += len(accounts)
    except Exception as e:
        node["accountsError"] = str(e)

    try:
        child_ous = _list_child_ous(client, node_id)
    except Exception as e:
        node["childrenError"] = str(e)
        return node

    if child_ous and depth >= MAX_OU_DEPTH:
        # Children exist beyond the documented nesting limit, so this walk
        # stopped short: say so instead of implying the map is complete. An OU
        # sitting exactly at the limit has no children, which is not truncation.
        stats["depth_truncated"] = True
        return node

    for ou in sorted(child_ous, key=lambda o: (o.get("Name") or "").lower()):
        ou_name = ou.get("Name", "")
        stats["organizational_units"] += 1
        node["children"].append(
            _build_node(
                client,
                ou.get("Id", ""),
                ou_name,
                "ORGANIZATIONAL_UNIT",
                f"{path}/{ou_name}",
                depth + 1,
                stats,
            )
        )
    return node


def _iter_nodes(node):
    """Yield a node and every descendant node, depth first."""
    yield node
    for child in node["children"]:
        yield from _iter_nodes(child)


def flatten_rows(org_map, include_accounts=True):
    """Flatten the org tree into display rows, parents before children.

    Each row is a dict with ``kind`` (``root``, ``ou``, or ``account``),
    ``name``, ``id``, ``depth``, ``path``, ``policies``, ``status``, and
    ``note``. An account row carries the path of the OU that contains it.
    """
    if not org_map or not org_map.get("root"):
        return []

    rows = []

    def visit(node):
        node_path = node.get("path", "")
        rows.append(
            {
                "kind": "root" if node.get("type") == "ROOT" else "ou",
                "name": node.get("name", ""),
                "id": node.get("id", ""),
                "depth": node.get("depth", 0),
                "path": node_path,
                "policies": node.get("policies") or [],
                "status": "",
                "note": _node_note(node),
            }
        )
        if include_accounts:
            for account in node.get("accounts") or []:
                rows.append(
                    {
                        "kind": "account",
                        "name": account.get("name", ""),
                        "id": account.get("id", ""),
                        "depth": node.get("depth", 0) + 1,
                        "path": node_path,
                        "policies": account.get("policies") or [],
                        "status": account.get("status", ""),
                        "note": "",
                    }
                )
        for child in node.get("children") or []:
            visit(child)

    visit(org_map["root"])
    return rows


def _node_note(node):
    """Join the per-node error messages recorded while building the map."""
    notes = [
        node.get(key) for key in ("accountsError", "childrenError") if node.get(key)
    ]
    return "; ".join(notes)


def _list_policies_of_type(client, policy_type):
    """List every policy of one type in the organization, fully paginated."""
    policies = []
    paginator = client.get_paginator("list_policies")
    for page in paginator.paginate(Filter=policy_type):
        policies.extend(page.get("Policies", []))
    return policies


def _list_targets_for_policy(client, policy_id):
    """List every root, OU, and account one policy is attached to."""
    targets = []
    paginator = client.get_paginator("list_targets_for_policy")
    for page in paginator.paginate(PolicyId=policy_id):
        targets.extend(page.get("Targets", []))
    return targets


def _attach_policies(client, root, policy_types):
    """Attach directly-attached policies to the tree.

    Returns a list of ``{scope, error}`` describing lookups that failed. An
    error means attachments may be incomplete; it is reported rather than
    silently producing an empty policy list.
    """
    targets_by_id = {}
    for node in _iter_nodes(root):
        if node["id"]:
            targets_by_id[node["id"]] = node
        for account in node["accounts"]:
            if account["id"]:
                targets_by_id[account["id"]] = account

    errors = []
    if not targets_by_id or not policy_types:
        return errors

    # One call per enabled policy type. The client is created by the caller in
    # the calling thread and shared, because ``boto3.client()`` uses the
    # default session, which is not safe to use from worker threads, while
    # botocore clients themselves are thread-safe. This matches
    # ``account_resources._scan_regions``.
    policies = []
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
        futures = {
            executor.submit(_list_policies_of_type, client, policy_type): policy_type
            for policy_type in policy_types
        }
        for future in as_completed(futures):
            policy_type = futures[future]
            try:
                policies.extend(future.result())
            except ClientError as e:
                if (
                    e.response.get("Error", {}).get("Code")
                    in _POLICY_TYPE_UNAVAILABLE_CODES
                ):
                    continue
                errors.append({"scope": policy_type, "error": str(e)})
            except Exception as e:
                errors.append({"scope": policy_type, "error": str(e)})

    # One call per policy. Account-level attachments arrive here too, so they
    # cost nothing extra.
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
        futures = {
            executor.submit(_list_targets_for_policy, client, policy.get("Id", "")): (
                policy
            )
            for policy in policies
            if policy.get("Id")
        }
        for future in as_completed(futures):
            policy = futures[future]
            summary = {
                "id": policy.get("Id", ""),
                "name": policy.get("Name", ""),
                "type": policy.get("Type", ""),
                "awsManaged": bool(policy.get("AwsManaged", False)),
            }
            try:
                attached_to = future.result()
            except Exception as e:
                errors.append(
                    {
                        "scope": f"{summary['type']}: {summary['name']}",
                        "error": str(e),
                    }
                )
                continue
            for target in attached_to:
                container = targets_by_id.get(target.get("TargetId"))
                if container is not None:
                    container["policies"].append(dict(summary))

    for container in targets_by_id.values():
        container["policies"].sort(
            key=lambda p: (p["type"], p["name"].lower(), p["id"])
        )
    errors.sort(key=lambda item: item["scope"])
    return errors


def get_organization_map():
    """FR-12: Map the OU hierarchy, account placement, and attached policies.

    Returns:
        tuple(dict | None, str | None): the map, and an error message when it
        could not be built. Both are ``None`` when the account is not in an
        organization, which is an absence rather than a failure.
    """
    stats = {
        "organizational_units": 0,
        "accounts": 0,
        "max_depth": 0,
        "depth_truncated": False,
    }
    try:
        client = boto3.client("organizations", config=_CLIENT_CONFIG)
        organization = client.describe_organization().get("Organization", {})

        roots = client.list_roots().get("Roots", [])
        if not roots:
            return None, "ListRoots returned no organization root."
        root = roots[0]
        enabled_policy_types = sorted(
            policy_type.get("Type")
            for policy_type in root.get("PolicyTypes", [])
            if policy_type.get("Status") == "ENABLED" and policy_type.get("Type")
        )

        root_name = root.get("Name") or "Root"
        root_node = _build_node(
            client, root.get("Id", ""), root_name, "ROOT", root_name, 0, stats
        )

        policy_errors = _attach_policies(client, root_node, enabled_policy_types)

        data = {
            "organizationId": organization.get("Id", ""),
            "managementAccountId": organization.get("MasterAccountId", ""),
            "root": root_node,
            "enabledPolicyTypes": enabled_policy_types,
            "policyTypesNotEnabled": [
                policy_type
                for policy_type in ALWAYS_REPORTED_POLICY_TYPES
                if policy_type not in enabled_policy_types
            ],
            "policyLookupErrors": policy_errors,
            "totals": {
                "organizationalUnits": stats["organizational_units"],
                "accounts": stats["accounts"],
                "maxDepth": stats["max_depth"],
            },
        }
        if stats["depth_truncated"]:
            data["depthTruncated"] = True
        return data, None

    except ClientError as e:
        if (
            e.response.get("Error", {}).get("Code")
            == "AWSOrganizationsNotInUseException"
        ):
            return None, None
        return None, str(e)
    except Exception as e:
        return None, str(e)
