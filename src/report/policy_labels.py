"""Display labels for AWS Organizations policy types (FR-12).

Single source of truth, imported by both the CSV export and the HTML report
payload. The report UI receives finished labels in the payload rather than
re-deriving them in JavaScript, so the CSV and the HTML from one run cannot
disagree.
"""

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
    """Return a short display label for an Organizations policy type.

    An unrecognized type, such as one AWS adds later, is title-cased so it
    still reads as a label rather than a constant.
    """
    if policy_type in _POLICY_TYPE_LABELS:
        return _POLICY_TYPE_LABELS[policy_type]
    return (policy_type or "").replace("_", " ").title()


def labelled_policies(policies):
    """Return policy dicts with a ``typeLabel`` added for display."""
    return [
        dict(policy, typeLabel=policy_type_label(policy.get("type")))
        for policy in policies or []
    ]
