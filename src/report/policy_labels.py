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


def lookup_error_scope(failure):
    """Return the display scope of one ``policyLookupErrors`` entry.

    ``wafa-raw.json`` keeps the raw ``scope`` (``TAG_POLICY`` or
    ``TAG_POLICY: Name``), like the raw ``type`` on each policy. Every display
    renders it through this function instead, so a failure reads
    ``Tag: Name`` beside the ``Tag: Name`` badge for the same policy.
    """
    policy_type = failure.get("policyType")
    if not policy_type:
        return failure.get("scope", "")
    label = policy_type_label(policy_type)
    name = failure.get("policyName")
    return f"{label}: {name}" if name else label


def pending_policy_type_warning(pending):
    """Describe one ``policyTypesPending`` entry for display.

    A type whose root status is ``PENDING_ENABLE`` or ``PENDING_DISABLE`` is
    changing state, so it is neither enabled nor absent: its policies are not
    listed and any attachments are left out of the map.
    """
    label = policy_type_label(pending.get("type", ""))
    status = pending.get("status", "")
    return (
        f"{label} is changing state ({status}), so its attachments are not "
        "mapped; re-run the assessment once it settles"
    )
