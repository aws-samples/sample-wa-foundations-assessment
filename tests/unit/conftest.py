"""Shared unit test fixtures."""

import pytest

from src.checks import org_api


@pytest.fixture(autouse=True)
def _reset_org_api_cache():
    """Clear the per-run Organizations cache around every test.

    ``src.checks.org_api`` caches ``DescribeOrganization`` and ``ListRoots``
    for the duration of an assessment run. Without this fixture one test's
    mocked response would answer another test's call, which would hide a
    missing ``boto3.client`` patch and make results depend on test order.
    """
    org_api.reset_cache()
    yield
    org_api.reset_cache()
