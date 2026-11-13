"""Shared read-only AWS Organizations reads for the assessment checks.

Two reasons this module exists:

- The pagination loops here were duplicated across modules. They take an
  existing client rather than creating one, so each caller keeps its own
  client and its own ``boto3.client`` test seam, and no scored check has to
  depend on an informational module to borrow a helper.
- ``DescribeOrganization`` and ``ListRoots`` answer the same question for every
  check that asks, because each check reads the organization independently. A
  single assessment run was making 2 ``DescribeOrganization`` and 8
  ``ListRoots`` calls against a service that throttles hard. The accessors
  below cache the answer for the run.

Only successful responses are cached. A failed call caches nothing, so one
denied or throttled read cannot turn into a failure for every later check that
would have succeeded — each check still determines its own status.

The cache lives for one assessment run. ``src.main.run`` clears it at the
start, and the unit tests clear it between tests, so a mocked response can
never leak from one test into the next.
"""

_CACHE = {}


def reset_cache():
    """Drop cached organization reads. Called once per assessment run."""
    _CACHE.clear()


def describe_organization(client):
    """Return the ``Organization`` dict, reading it at most once per run."""
    if "organization" not in _CACHE:
        response = client.describe_organization()
        _CACHE["organization"] = response.get("Organization", {})
    return _CACHE["organization"]


def list_roots(client):
    """Return the organization roots, reading them at most once per run.

    The root carries the enabled policy types, which several checks and the
    organization map all need.
    """
    if "roots" not in _CACHE:
        _CACHE["roots"] = client.list_roots().get("Roots", [])
    return _CACHE["roots"]


def list_child_ous(client, parent_id):
    """List OUs directly under a root or OU, fully paginated.

    Not cached: the organization map walks every parent, so there is no single
    answer to reuse.
    """
    ous = []
    paginator = client.get_paginator("list_organizational_units_for_parent")
    for page in paginator.paginate(ParentId=parent_id):
        ous.extend(page.get("OrganizationalUnits", []))
    return ous


def list_child_accounts(client, parent_id):
    """List accounts directly under a root or OU, fully paginated."""
    accounts = []
    paginator = client.get_paginator("list_accounts_for_parent")
    for page in paginator.paginate(ParentId=parent_id):
        accounts.extend(page.get("Accounts", []))
    return accounts
