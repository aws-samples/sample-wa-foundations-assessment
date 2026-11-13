"""Organization Governance Checks (FR-3).

Checks AWS Organizations structure: org exists, accounts, OUs, policies.
Only runs if the current account is the management account.
"""

import re

import boto3
from botocore.exceptions import ClientError

from src.checks import org_api


def _normalize_name(name):
    """Normalize an account or OU name for matching.

    Ignores case, spaces, hyphens, and underscores so that, for example,
    "Log Archive", "LogArchive", and "log-archive" all match.
    """
    return re.sub(r"[\s_-]+", "", name or "").lower()


def _is_active(account):
    """Return True unless the account is suspended, closing, or closed.

    Prefer State (the newer field) and fall back to Status.
    """
    state = account.get("State") or account.get("Status") or "ACTIVE"
    return state.upper() == "ACTIVE"


def _list_active_accounts(client):
    """List organization accounts that are active."""
    accounts = []
    paginator = client.get_paginator("list_accounts")
    for page in paginator.paginate():
        accounts.extend(a for a in page.get("Accounts", []) if _is_active(a))
    return accounts


def check_org_exists():
    """FR-3.1: Verify an AWS Organization exists."""
    result = {
        "check": "AWS Organization exists",
        "description": "An AWS Organization should be established for multi-account management.",
        "status": "incomplete",
        "required": True,
        "weight": 6,
        "loe": 1,
        "remediationLink": "https://docs.aws.amazon.com/organizations/latest/userguide/orgs_manage_org_create.html",
    }
    try:
        client = boto3.client("organizations")
        organization = org_api.describe_organization(client)
        result["status"] = "complete" if organization.get("Id") else "incomplete"
        return result
    except ClientError as e:
        # A standalone account legitimately has no organization — that is an
        # "incomplete" gap to close, not an error. Any other API failure
        # (AccessDenied, throttling, etc.) leaves the posture unknown → error.
        if e.response["Error"]["Code"] == "AWSOrganizationsNotInUseException":
            result["status"] = "incomplete"
            return result
        result["status"] = "error"
        result["error"] = str(e)
        return result
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
        return result


def check_management_account(account_info):
    """FR-3.2: Verify running from the management account."""
    is_mgmt = account_info.get("is_management_account", False)
    result = {
        "check": "Management account identified",
        "description": "Assessment should be run from the organization management account.",
        "status": "complete" if is_mgmt else "incomplete",
        "required": True,
        "weight": 6,
        "loe": 1,
        "remediationLink": "https://docs.aws.amazon.com/organizations/latest/userguide/orgs_manage_accounts.html",
    }
    if account_info.get("detection_error"):
        result["status"] = "error"
        result["error"] = account_info["detection_error"]
    return result


def check_minimum_accounts():
    """FR-3.3: Verify at least 4 accounts exist in the organization."""
    try:
        client = boto3.client("organizations")
        accounts = _list_active_accounts(client)

        return {
            "check": "Minimum 4 accounts",
            "description": "Organization should have at least 4 accounts (Management, Log Archive, Audit, Workload).",
            "status": "complete" if len(accounts) >= 4 else "incomplete",
            "required": True,
            "weight": 6,
            "loe": 2,
            "remediationLink": "https://docs.aws.amazon.com/whitepapers/latest/organizing-your-aws-environment/recommended-ous-and-accounts.html",
        }
    except Exception as e:
        return {
            "check": "Minimum 4 accounts",
            "description": "Organization should have at least 4 accounts (Management, Log Archive, Audit, Workload).",
            "status": "error",
            "required": True,
            "weight": 6,
            "loe": 2,
            "remediationLink": "https://docs.aws.amazon.com/whitepapers/latest/organizing-your-aws-environment/recommended-ous-and-accounts.html",
            "error": str(e),
        }


def check_log_archive_account():
    """FR-3.4: Verify a Log Archive account exists."""
    try:
        client = boto3.client("organizations")
        accounts = _list_active_accounts(client)

        found = any(_normalize_name(a.get("Name")) == "logarchive" for a in accounts)
        return {
            "check": "Log Archive account exists",
            "description": "A dedicated Log Archive account should exist for centralized logging.",
            "status": "complete" if found else "incomplete",
            "required": True,
            "weight": 6,
            "loe": 2,
            "remediationLink": "https://docs.aws.amazon.com/prescriptive-guidance/latest/security-reference-architecture/log-archive.html",
        }
    except Exception as e:
        return {
            "check": "Log Archive account exists",
            "description": "A dedicated Log Archive account should exist for centralized logging.",
            "status": "error",
            "required": True,
            "weight": 6,
            "loe": 2,
            "remediationLink": "https://docs.aws.amazon.com/prescriptive-guidance/latest/security-reference-architecture/log-archive.html",
            "error": str(e),
        }


def check_audit_account():
    """FR-3.5: Verify an Audit/Security Tooling account exists."""
    try:
        client = boto3.client("organizations")
        accounts = _list_active_accounts(client)

        found = any(
            _normalize_name(a.get("Name")) in ("audit", "securitytooling")
            for a in accounts
        )
        return {
            "check": "Audit account exists",
            "description": "A dedicated Audit or Security Tooling account should exist.",
            "status": "complete" if found else "incomplete",
            "required": True,
            "weight": 6,
            "loe": 2,
            "remediationLink": "https://docs.aws.amazon.com/prescriptive-guidance/latest/security-reference-architecture/security-tooling.html",
        }
    except Exception as e:
        return {
            "check": "Audit account exists",
            "description": "A dedicated Audit or Security Tooling account should exist.",
            "status": "error",
            "required": True,
            "weight": 6,
            "loe": 2,
            "remediationLink": "https://docs.aws.amazon.com/prescriptive-guidance/latest/security-reference-architecture/security-tooling.html",
            "error": str(e),
        }


def _get_policy_types():
    """Helper: get policy types from organization root.

    Four checks ask for this and three more ask for the root's id. The root is
    read once per run through ``org_api`` rather than once per check.
    """
    client = boto3.client("organizations")
    roots = org_api.list_roots(client)
    if not roots:
        return []
    return roots[0].get("PolicyTypes", [])


def check_scp_enabled():
    """FR-3.6: Verify Service Control Policies are enabled."""
    try:
        policy_types = _get_policy_types()
        enabled = any(
            pt.get("Type") == "SERVICE_CONTROL_POLICY" and pt.get("Status") == "ENABLED"
            for pt in policy_types
        )
        return {
            "check": "Service Control Policies enabled",
            "description": "SCPs should be enabled to enforce guardrails across the organization.",
            "status": "complete" if enabled else "incomplete",
            "required": True,
            "weight": 6,
            "loe": 1,
            "remediationLink": "https://docs.aws.amazon.com/organizations/latest/userguide/orgs_manage_policies_scps.html",
        }
    except Exception as e:
        return {
            "check": "Service Control Policies enabled",
            "description": "SCPs should be enabled to enforce guardrails across the organization.",
            "status": "error",
            "required": True,
            "weight": 6,
            "loe": 1,
            "remediationLink": "https://docs.aws.amazon.com/organizations/latest/userguide/orgs_manage_policies_scps.html",
            "error": str(e),
        }


def check_tag_policy_enabled():
    """FR-3.7: Verify Tag Policies are enabled."""
    try:
        policy_types = _get_policy_types()
        enabled = any(
            pt.get("Type") == "TAG_POLICY" and pt.get("Status") == "ENABLED"
            for pt in policy_types
        )
        return {
            "check": "Tag Policies enabled",
            "description": "Tag Policies should be enabled for consistent resource tagging and cost allocation.",
            "status": "complete" if enabled else "incomplete",
            "required": True,
            "weight": 6,
            "loe": 1,
            "remediationLink": "https://docs.aws.amazon.com/organizations/latest/userguide/orgs_manage_policies_tag-policies.html",
        }
    except Exception as e:
        return {
            "check": "Tag Policies enabled",
            "description": "Tag Policies should be enabled for consistent resource tagging and cost allocation.",
            "status": "error",
            "required": True,
            "weight": 6,
            "loe": 1,
            "remediationLink": "https://docs.aws.amazon.com/organizations/latest/userguide/orgs_manage_policies_tag-policies.html",
            "error": str(e),
        }


def check_backup_policy_enabled():
    """FR-3.8: Verify Backup Policies are enabled."""
    try:
        policy_types = _get_policy_types()
        enabled = any(
            pt.get("Type") == "BACKUP_POLICY" and pt.get("Status") == "ENABLED"
            for pt in policy_types
        )
        return {
            "check": "Backup Policies enabled",
            "description": "Backup Policies should be enabled for centralized backup governance.",
            "status": "complete" if enabled else "incomplete",
            "required": False,
            "weight": 5,
            "loe": 1,
            "remediationLink": "https://docs.aws.amazon.com/organizations/latest/userguide/orgs_manage_policies_backup.html",
        }
    except Exception as e:
        return {
            "check": "Backup Policies enabled",
            "description": "Backup Policies should be enabled for centralized backup governance.",
            "status": "error",
            "required": False,
            "weight": 5,
            "loe": 1,
            "remediationLink": "https://docs.aws.amazon.com/organizations/latest/userguide/orgs_manage_policies_backup.html",
            "error": str(e),
        }


def check_rcp_enabled():
    """FR-3.9: Verify Resource Control Policies are enabled."""
    try:
        policy_types = _get_policy_types()
        enabled = any(
            pt.get("Type") == "RESOURCE_CONTROL_POLICY"
            and pt.get("Status") == "ENABLED"
            for pt in policy_types
        )
        return {
            "check": "Resource Control Policies enabled",
            "description": "RCPs should be enabled for data perimeter controls.",
            "status": "complete" if enabled else "incomplete",
            "required": False,
            "weight": 4,
            "loe": 1,
            "remediationLink": "https://docs.aws.amazon.com/organizations/latest/userguide/orgs_manage_policies_rcps.html",
        }
    except Exception as e:
        return {
            "check": "Resource Control Policies enabled",
            "description": "RCPs should be enabled for data perimeter controls.",
            "status": "error",
            "required": False,
            "weight": 4,
            "loe": 1,
            "remediationLink": "https://docs.aws.amazon.com/organizations/latest/userguide/orgs_manage_policies_rcps.html",
            "error": str(e),
        }


def _get_top_level_ous():
    """Helper: get top-level OUs under the root."""
    client = boto3.client("organizations")
    roots = org_api.list_roots(client)
    if not roots:
        return []

    return org_api.list_child_ous(client, roots[0]["Id"])


def check_security_ou():
    """FR-3.10: Verify a Security OU exists at root level."""
    try:
        ous = _get_top_level_ous()
        found = any(_normalize_name(ou.get("Name")) == "security" for ou in ous)
        return {
            "check": "Security OU exists",
            "description": "A Security OU should exist at the root level for security accounts.",
            "status": "complete" if found else "incomplete",
            "required": True,
            "weight": 6,
            "loe": 2,
            "remediationLink": "https://docs.aws.amazon.com/whitepapers/latest/organizing-your-aws-environment/foundational-ous.html#security-ou",
        }
    except Exception as e:
        return {
            "check": "Security OU exists",
            "description": "A Security OU should exist at the root level for security accounts.",
            "status": "error",
            "required": True,
            "weight": 6,
            "loe": 2,
            "remediationLink": "https://docs.aws.amazon.com/whitepapers/latest/organizing-your-aws-environment/foundational-ous.html#security-ou",
            "error": str(e),
        }


def check_workloads_ou():
    """FR-3.11: Verify a Workloads OU exists at root level."""
    try:
        ous = _get_top_level_ous()
        found = any(_normalize_name(ou.get("Name")) == "workloads" for ou in ous)
        return {
            "check": "Workloads OU exists",
            "description": "A Workloads OU should exist for business application accounts.",
            "status": "complete" if found else "incomplete",
            "required": False,
            "weight": 5,
            "loe": 2,
            "remediationLink": "https://docs.aws.amazon.com/whitepapers/latest/organizing-your-aws-environment/application-ous.html#workloads-ou",
        }
    except Exception as e:
        return {
            "check": "Workloads OU exists",
            "description": "A Workloads OU should exist for business application accounts.",
            "status": "error",
            "required": False,
            "weight": 5,
            "loe": 2,
            "remediationLink": "https://docs.aws.amazon.com/whitepapers/latest/organizing-your-aws-environment/application-ous.html#workloads-ou",
            "error": str(e),
        }


def check_infrastructure_ou():
    """FR-3.12: Verify an Infrastructure OU exists at root level."""
    try:
        ous = _get_top_level_ous()
        found = any(_normalize_name(ou.get("Name")) == "infrastructure" for ou in ous)
        return {
            "check": "Infrastructure OU exists",
            "description": "An Infrastructure OU should exist for shared infrastructure services.",
            "status": "complete" if found else "incomplete",
            "required": False,
            "weight": 5,
            "loe": 2,
            "remediationLink": "https://docs.aws.amazon.com/whitepapers/latest/organizing-your-aws-environment/foundational-ous.html#infrastructure-ou",
        }
    except Exception as e:
        return {
            "check": "Infrastructure OU exists",
            "description": "An Infrastructure OU should exist for shared infrastructure services.",
            "status": "error",
            "required": False,
            "weight": 5,
            "loe": 2,
            "remediationLink": "https://docs.aws.amazon.com/whitepapers/latest/organizing-your-aws-environment/foundational-ous.html#infrastructure-ou",
            "error": str(e),
        }


def run_all():
    """Run all organization governance checks (FR-3.3 through FR-3.12).

    FR-3.1 and FR-3.2 are handled separately (called from main.py with account_info).
    """
    return [
        check_minimum_accounts(),
        check_log_archive_account(),
        check_audit_account(),
        check_scp_enabled(),
        check_tag_policy_enabled(),
        check_backup_policy_enabled(),
        check_rcp_enabled(),
        check_security_ou(),
        check_workloads_ou(),
        check_infrastructure_ou(),
    ]
