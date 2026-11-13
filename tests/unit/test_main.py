"""Integration-style unit tests for src/main.py — orchestrator end-to-end.

Mocks all boto3 calls to simulate a full assessment run without real AWS credentials.
Tests both management account (full) and non-management account (limited) flows.
"""

import csv
import io
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.checks.org_map import flatten_rows
from src.main import (
    MAX_ORG_MAP_CONSOLE_WARNINGS,
    exit_code,
    print_org_map_summary,
    print_summary,
    run,
    upload_to_s3,
)


def test_deployment_names_use_wa_foundations_prefix():
    """Future deployments consistently use the WA Foundations resource prefix."""
    repository_root = Path(__file__).resolve().parents[2]
    template = (repository_root / "deployment" / "wafa-stack.yaml").read_text()
    deploy_script = (repository_root / "deploy.sh").read_text()

    assert "Name: !Ref 'AWS::StackName'" in template
    assert "/aws/codebuild/WAFA-${AWS::StackName}" not in template
    assert "'wa-foundations-results-'" in template
    assert "Default: 'wa-foundations-source.zip'" in template
    assert "Value: WA-Foundations-Assessment" in template
    assert (
        "Deploys the Well-Architected Foundations Assessment for AWS Organizations"
        in template
    )
    assert "automated AWS CodeBuild execution" in template
    assert "scoped IAM permissions" in template
    assert "versioned Amazon S3 report storage" in template
    assert 'STACK_NAME="wa-foundations-assessment"' in deploy_script
    assert 'SOURCE_BUCKET="wa-foundations-source-${ACCOUNT_ID}-${REGION}"' in (
        deploy_script
    )
    assert 'SOURCE_KEY="wa-foundations-source.zip"' in deploy_script
    assert (
        "Description: Name of the S3 bucket containing generated assessment reports"
        in template
    )
    assert (
        "Description: Name of the CodeBuild project used to run or re-run the assessment"
        in template
    )
    assert 'PROJECT_NAME="$STACK_NAME"' in deploy_script
    assert 'STACK_NAME="wafa"' not in deploy_script
    assert 'PROJECT_NAME="WAFA-${STACK_NAME}"' not in deploy_script


def _run_deploy_with_fake_aws(tmp_path, extra_env=None):
    """Run deploy.sh against a fake aws CLI and return (result, call log)."""
    repository_root = Path(__file__).resolve().parents[2]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "aws-calls.log"

    fake_aws = fake_bin / "aws"
    fake_aws.write_text(
        """#!/bin/bash
set -eu
printf '%s\\n' "$*" >> "$AWS_CALL_LOG"
case "$1 $2" in
    "sts get-caller-identity")
        echo "111111111111"
        ;;
    "s3api head-bucket")
        case "${FAKE_HEAD_BUCKET:-owned}" in
            owned) ;;
            missing) echo "An error occurred (404) when calling the HeadBucket operation: Not Found" >&2; exit 254 ;;
            foreign) echo "An error occurred (403) when calling the HeadBucket operation: Forbidden" >&2; exit 254 ;;
        esac
        ;;
    "s3 mb"|"s3api put-object"|"s3 ls"|"cloudformation deploy")
        ;;
    "codebuild start-build")
        echo "wa-foundations-assessment:deployment-build"
        ;;
    "cloudformation describe-stacks")
        echo "wa-foundations-results-example"
        ;;
    "codebuild batch-get-builds")
        case " $* " in
            *" --ids wa-foundations-assessment:deployment-build "*)
                echo "SUCCEEDED"
                ;;
            *)
                echo "batch-get-builds used the wrong build ID: $*" >&2
                exit 42
                ;;
        esac
        ;;
    *)
        echo "Unexpected AWS command: $*" >&2
        exit 99
        ;;
esac
""",
        encoding="utf-8",
    )
    fake_aws.chmod(0o755)

    fake_zip = fake_bin / "zip"
    fake_zip.write_text(
        '#!/bin/bash\nset -eu\n: > "$2"\n',
        encoding="utf-8",
    )
    fake_zip.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["AWS_CALL_LOG"] = str(call_log)
    env.update(extra_env or {})
    result = subprocess.run(
        ["bash", "deploy.sh", "--profile", "test-profile", "--region", "us-east-1"],
        cwd=repository_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    calls = call_log.read_text(encoding="utf-8") if call_log.exists() else ""
    return result, calls


def test_deploy_waits_for_build_id_returned_by_start_build(tmp_path):
    """Deployment waits on its own build even when other builds may exist."""
    result, calls = _run_deploy_with_fake_aws(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert calls.count("codebuild start-build ") == 1
    assert "codebuild list-builds-for-project" not in calls
    assert "AutoStartBuild=false" in calls
    assert (
        "codebuild batch-get-builds "
        "--ids wa-foundations-assessment:deployment-build" in calls
    )
    assert "Build: wa-foundations-assessment:deployment-build" in result.stdout
    assert "--expected-bucket-owner 111111111111" in calls
    assert "s3 mb" not in calls


def test_deploy_creates_missing_source_bucket(tmp_path):
    result, calls = _run_deploy_with_fake_aws(tmp_path, {"FAKE_HEAD_BUCKET": "missing"})

    assert result.returncode == 0, result.stdout + result.stderr
    assert "s3 mb s3://wa-foundations-source-111111111111-us-east-1" in calls
    assert "s3api put-object" in calls


def test_deploy_refuses_source_bucket_owned_by_another_account(tmp_path):
    """A same-named bucket in another account must never receive source code."""
    result, calls = _run_deploy_with_fake_aws(tmp_path, {"FAKE_HEAD_BUCKET": "foreign"})

    assert result.returncode == 1
    assert "Refusing to upload source code" in result.stdout
    assert "s3api put-object" not in calls
    assert "s3 mb" not in calls
    assert "cloudformation deploy" not in calls


# ============================================================================
# Shared mock fixtures
# ============================================================================


def _mock_management_account_boto3():
    """Build a mock boto3.client that simulates a well-configured management account."""

    def client_factory(service, **kwargs):
        mock = MagicMock()

        if service == "sts":
            mock.get_caller_identity.return_value = {"Account": "111111111111"}

        elif service == "organizations":
            mock.describe_organization.return_value = {
                "Organization": {"Id": "o-abc123", "MasterAccountId": "111111111111"}
            }
            # list_accounts paginator
            accounts_paginator = MagicMock()
            accounts_paginator.paginate.return_value = [
                {
                    "Accounts": [
                        {"Id": "111111111111", "Name": "Management"},
                        {"Id": "222222222222", "Name": "Log Archive"},
                        {"Id": "333333333333", "Name": "Audit"},
                        {"Id": "444444444444", "Name": "Workload-Prod"},
                    ]
                }
            ]
            # list_aws_service_access paginator
            svc_paginator = MagicMock()
            svc_paginator.paginate.return_value = [
                {
                    "EnabledServicePrincipals": [
                        {"ServicePrincipal": "cloudtrail.amazonaws.com"},
                        {"ServicePrincipal": "config.amazonaws.com"},
                        {"ServicePrincipal": "guardduty.amazonaws.com"},
                        {"ServicePrincipal": "securityhub.amazonaws.com"},
                        {"ServicePrincipal": "access-analyzer.amazonaws.com"},
                        {"ServicePrincipal": "ram.amazonaws.com"},
                        {"ServicePrincipal": "sso.amazonaws.com"},
                        {
                            "ServicePrincipal": "member.org.stacksets.cloudformation.amazonaws.com"
                        },
                        {"ServicePrincipal": "backup.amazonaws.com"},
                        {"ServicePrincipal": "cost-optimization-hub.bcm.amazonaws.com"},
                    ]
                }
            ]
            # list_organizational_units paginator, keyed by parent so that the
            # FR-12 org map walks a finite hierarchy rather than finding the
            # same OUs beneath every parent.
            child_ous = {
                "r-root": [
                    {"Id": "ou-sec", "Name": "Security"},
                    {"Id": "ou-wl", "Name": "Workloads"},
                    {"Id": "ou-infra", "Name": "Infrastructure"},
                ]
            }
            ou_paginator = MagicMock()
            ou_paginator.paginate.side_effect = lambda ParentId: [
                {"OrganizationalUnits": child_ous.get(ParentId, [])}
            ]

            # FR-12: accounts per parent and policies attached per target.
            accounts_for_parent = {
                "r-root": [
                    {"Id": "111111111111", "Name": "Management", "Status": "ACTIVE"}
                ],
                "ou-sec": [
                    {"Id": "222222222222", "Name": "Log Archive", "Status": "ACTIVE"},
                    {"Id": "333333333333", "Name": "Audit", "Status": "ACTIVE"},
                ],
                "ou-wl": [
                    {
                        "Id": "444444444444",
                        "Name": "Workload-Prod",
                        "Status": "ACTIVE",
                    }
                ],
            }
            afp_paginator = MagicMock()
            afp_paginator.paginate.side_effect = lambda ParentId: [
                {"Accounts": accounts_for_parent.get(ParentId, [])}
            ]

            # FR-12.3: policies are enumerated by type, then located by target.
            policies_by_type = {
                "SERVICE_CONTROL_POLICY": [
                    {
                        "Id": "p-FullAWSAccess",
                        "Name": "FullAWSAccess",
                        "Type": "SERVICE_CONTROL_POLICY",
                        "AwsManaged": True,
                    },
                    {
                        "Id": "p-denyroot",
                        "Name": "DenyRootUserActions",
                        "Type": "SERVICE_CONTROL_POLICY",
                        "AwsManaged": False,
                    },
                ],
                "RESOURCE_CONTROL_POLICY": [
                    {
                        "Id": "p-perimeter",
                        "Name": "EnforceDataPerimeter",
                        "Type": "RESOURCE_CONTROL_POLICY",
                        "AwsManaged": False,
                    }
                ],
            }
            lp_paginator = MagicMock()
            lp_paginator.paginate.side_effect = lambda Filter: [
                {"Policies": policies_by_type.get(Filter, [])}
            ]
            targets_by_policy = {
                "p-FullAWSAccess": [
                    {"TargetId": "r-root", "Type": "ROOT", "Name": "Root"}
                ],
                "p-denyroot": [
                    {
                        "TargetId": "ou-sec",
                        "Type": "ORGANIZATIONAL_UNIT",
                        "Name": "Security",
                    },
                    {
                        "TargetId": "444444444444",
                        "Type": "ACCOUNT",
                        "Name": "Workload-Prod",
                    },
                ],
                "p-perimeter": [
                    {
                        "TargetId": "ou-wl",
                        "Type": "ORGANIZATIONAL_UNIT",
                        "Name": "Workloads",
                    }
                ],
            }
            tfp_paginator = MagicMock()
            tfp_paginator.paginate.side_effect = lambda PolicyId: [
                {"Targets": targets_by_policy.get(PolicyId, [])}
            ]
            # delegated admins paginator
            da_paginator = MagicMock()
            da_paginator.paginate.return_value = [
                {
                    "DelegatedAdministrators": [
                        {"Id": "333333333333", "Name": "Audit"},
                    ]
                }
            ]
            # delegated services paginator
            ds_paginator = MagicMock()
            ds_paginator.paginate.return_value = [
                {
                    "DelegatedServices": [
                        {"ServicePrincipal": "guardduty.amazonaws.com"},
                    ]
                }
            ]

            def get_paginator(name):
                if name == "list_accounts":
                    return accounts_paginator
                elif name == "list_aws_service_access_for_organization":
                    return svc_paginator
                elif name == "list_organizational_units_for_parent":
                    return ou_paginator
                elif name == "list_accounts_for_parent":
                    return afp_paginator
                elif name == "list_policies":
                    return lp_paginator
                elif name == "list_targets_for_policy":
                    return tfp_paginator
                elif name == "list_delegated_administrators":
                    return da_paginator
                elif name == "list_delegated_services_for_account":
                    return ds_paginator
                return MagicMock()

            mock.get_paginator.side_effect = get_paginator
            mock.list_roots.return_value = {
                "Roots": [
                    {
                        "Id": "r-root",
                        "Name": "Root",
                        "PolicyTypes": [
                            {"Type": "SERVICE_CONTROL_POLICY", "Status": "ENABLED"},
                            {"Type": "TAG_POLICY", "Status": "ENABLED"},
                            {"Type": "BACKUP_POLICY", "Status": "ENABLED"},
                            {"Type": "RESOURCE_CONTROL_POLICY", "Status": "ENABLED"},
                        ],
                    }
                ]
            }

        elif service == "controltower":
            mock.list_landing_zones.return_value = {
                "landingZones": [
                    {"arn": "arn:aws:controltower:us-east-1:111:landingzone/lz1"}
                ]
            }
            mock.get_landing_zone.return_value = {
                "landingZone": {
                    "driftStatus": {"status": "IN_SYNC"},
                    "version": "3.3",
                    "latestAvailableVersion": "3.3",
                }
            }

        elif service == "sso-admin":
            mock.list_instances.return_value = {
                "Instances": [{"InstanceArn": "arn:aws:sso:::instance/ssoins-123"}]
            }

        elif service == "iam":
            mock.get_paginator.return_value.paginate.return_value = [{"Users": []}]

        elif service == "ec2":
            mock.describe_regions.return_value = {
                "Regions": [
                    {"RegionName": "us-east-1"},
                    {"RegionName": "us-west-2"},
                ]
            }
            mock.describe_instances.return_value = {"Reservations": []}
            mock.describe_vpcs.return_value = {"Vpcs": []}

        elif service == "cloudtrail":
            mock.get_trail_status.return_value = {"IsLogging": True}
            mock.describe_trails.return_value = {
                "trailList": [
                    {
                        "Name": "org-trail",
                        "IsOrganizationTrail": True,
                        "HomeRegion": "us-east-1",
                    }
                ]
            }

        elif service == "config":
            mock.describe_configuration_recorders.return_value = {
                "ConfigurationRecorders": [{"name": "default"}]
            }
            mock.describe_configuration_recorder_status.return_value = {
                "ConfigurationRecordersStatus": [{"name": "default", "recording": True}]
            }
            mock.describe_delivery_channels.return_value = {
                "DeliveryChannels": [{"name": "default"}]
            }
            mock.describe_delivery_channel_status.return_value = {
                "DeliveryChannelsStatus": [
                    {
                        "name": "default",
                        "configHistoryDeliveryInfo": {"lastStatus": "SUCCESS"},
                    }
                ]
            }

        elif service == "cur":
            mock.describe_report_definitions.return_value = {
                "ReportDefinitions": [{"ReportName": "wafa-cur"}]
            }

        elif service == "cloudformation":
            mock.describe_organizations_access.return_value = {"Status": "ENABLED"}

        elif service == "s3":
            mock.put_object.return_value = {}

        return mock

    return client_factory


def _mock_member_account_boto3():
    """Build a mock boto3.client that simulates a member account."""

    def client_factory(service, **kwargs):
        mock = MagicMock()

        if service == "sts":
            mock.get_caller_identity.return_value = {"Account": "222222222222"}

        elif service == "organizations":
            mock.describe_organization.return_value = {
                "Organization": {"Id": "o-abc123", "MasterAccountId": "111111111111"}
            }

        elif service == "ec2":
            mock.describe_regions.return_value = {
                "Regions": [{"RegionName": "us-east-1"}]
            }
            mock.describe_instances.return_value = {"Reservations": []}
            mock.describe_vpcs.return_value = {
                "Vpcs": [{"VpcId": "vpc-default", "IsDefault": True}]
            }

        elif service == "iam":
            mock.get_paginator.return_value.paginate.return_value = [
                {"Users": [{"UserName": "legacy-user"}]}
            ]

        elif service == "cloudtrail":
            mock.get_trail_status.return_value = {"IsLogging": True}
            mock.describe_trails.return_value = {
                "trailList": [{"Name": "trail", "IsOrganizationTrail": False}]
            }

        elif service == "config":
            mock.describe_configuration_recorders.return_value = {
                "ConfigurationRecorders": [{"name": "default"}]
            }
            mock.describe_configuration_recorder_status.return_value = {
                "ConfigurationRecordersStatus": [{"name": "default", "recording": True}]
            }
            mock.describe_delivery_channels.return_value = {
                "DeliveryChannels": [{"name": "default"}]
            }
            mock.describe_delivery_channel_status.return_value = {
                "DeliveryChannelsStatus": [
                    {
                        "name": "default",
                        "configHistoryDeliveryInfo": {"lastStatus": "SUCCESS"},
                    }
                ]
            }

        return mock

    return client_factory


def _mock_standalone_account_boto3():
    """Build a mock boto3.client that simulates a standalone account (no org)."""
    from botocore.exceptions import ClientError

    def client_factory(service, **kwargs):
        mock = MagicMock()

        if service == "sts":
            mock.get_caller_identity.return_value = {"Account": "333333333333"}

        elif service == "organizations":
            mock.describe_organization.side_effect = ClientError(
                {
                    "Error": {
                        "Code": "AWSOrganizationsNotInUseException",
                        "Message": "Not in org",
                    }
                },
                "DescribeOrganization",
            )

        elif service == "ec2":
            mock.describe_regions.return_value = {
                "Regions": [{"RegionName": "us-east-1"}]
            }
            mock.describe_instances.return_value = {"Reservations": []}
            mock.describe_vpcs.return_value = {
                "Vpcs": [{"VpcId": "vpc-default", "IsDefault": True}]
            }

        elif service == "iam":
            mock.get_paginator.return_value.paginate.return_value = [
                {"Users": [{"UserName": "legacy-user"}]}
            ]

        elif service == "cloudtrail":
            mock.get_trail_status.return_value = {"IsLogging": True}
            mock.describe_trails.return_value = {
                "trailList": [{"Name": "trail", "IsOrganizationTrail": False}]
            }

        elif service == "config":
            mock.describe_configuration_recorders.return_value = {
                "ConfigurationRecorders": [{"name": "default"}]
            }
            mock.describe_configuration_recorder_status.return_value = {
                "ConfigurationRecordersStatus": [{"name": "default", "recording": True}]
            }
            mock.describe_delivery_channels.return_value = {
                "DeliveryChannels": [{"name": "default"}]
            }
            mock.describe_delivery_channel_status.return_value = {
                "DeliveryChannelsStatus": [
                    {
                        "name": "default",
                        "configHistoryDeliveryInfo": {"lastStatus": "SUCCESS"},
                    }
                ]
            }

        return mock

    return client_factory


# ============================================================================
# Management account — full assessment
# ============================================================================


class TestRunManagementAccount:
    """End-to-end test: management account runs all checks."""

    @patch("src.main.parse_args")
    @patch("src.checks.account_resources.boto3.client")
    @patch("src.checks.identity.boto3.client")
    @patch("src.checks.control_tower.boto3.client")
    @patch("src.checks.org_services.boto3.client")
    @patch("src.checks.organization.boto3.client")
    @patch("src.checks.org_map.boto3.client")
    @patch("src.checks.delegated_admin.boto3.client")
    @patch("src.discovery.regions.boto3.client")
    @patch("src.discovery.account_type.boto3.client")
    def test_full_assessment_produces_reports(
        self,
        mock_acct_client,
        mock_regions_client,
        mock_da_client,
        mock_org_map_client,
        mock_org_client,
        mock_svc_client,
        mock_ct_client,
        mock_id_client,
        mock_res_client,
        mock_parse_args,
        tmp_path,
    ):
        """Full run: management account → all checks → reports written to disk."""
        output_dir = str(tmp_path)
        mock_parse_args.return_value = MagicMock(output=output_dir)

        factory = _mock_management_account_boto3()
        mock_acct_client.side_effect = factory
        mock_regions_client.side_effect = factory
        mock_da_client.side_effect = factory
        mock_org_map_client.side_effect = factory
        mock_org_client.side_effect = factory
        mock_svc_client.side_effect = factory
        mock_ct_client.side_effect = factory
        mock_id_client.side_effect = factory
        mock_res_client.side_effect = factory

        checks, maturity = run()

        # Verify checks were produced
        assert len(checks) > 30  # Full assessment has 35 checks
        assert all("check" in c for c in checks)
        assert all("status" in c for c in checks)
        assert all(c["status"] in ("complete", "incomplete", "error") for c in checks)

        # Verify maturity was calculated
        assert maturity["level"] in (1, 2, 3, 4, 5)
        assert "name" in maturity

        # Verify reports were written
        assert os.path.exists(os.path.join(output_dir, "wafa-report.html"))
        assert os.path.exists(os.path.join(output_dir, "wafa-checks.csv"))
        assert os.path.exists(os.path.join(output_dir, "wafa-raw.json"))

        # FR-12: the organization map CSV accompanies the reports
        assert os.path.exists(os.path.join(output_dir, "wafa-org-map.csv"))

        # Verify HTML content
        with open(os.path.join(output_dir, "wafa-report.html")) as f:
            html = f.read()
        assert 'id="wafa-report-root"' in html
        assert 'id="wafa-report-data"' in html
        assert "AWS Organization exists" in html

        # Verify CSV content
        with open(os.path.join(output_dir, "wafa-checks.csv")) as f:
            csv_content = f.read()
        assert "Check Name" in csv_content
        assert "complete" in csv_content

        # Verify JSON content
        with open(os.path.join(output_dir, "wafa-raw.json")) as f:
            raw = json.load(f)
        assert "checks" in raw
        assert "maturity" in raw
        assert "account_info" in raw
        assert raw["account_info"]["account_id"] == "111111111111"

        # FR-12: the map is genuinely populated, not an error placeholder
        assert raw["organization_map_error"] is None
        org_map = raw["organization_map"]
        assert org_map["organizationId"] == "o-abc123"
        assert org_map["totals"] == {
            "organizationalUnits": 3,
            "accounts": 4,
            "maxDepth": 1,
        }
        assert [ou["name"] for ou in org_map["root"]["children"]] == [
            "Infrastructure",
            "Security",
            "Workloads",
        ]
        assert org_map["policyLookupErrors"] == []
        security = next(
            ou for ou in org_map["root"]["children"] if ou["id"] == "ou-sec"
        )
        assert [a["name"] for a in security["accounts"]] == ["Audit", "Log Archive"]
        assert [p["name"] for p in security["policies"]] == ["DenyRootUserActions"]

    @patch("src.main.parse_args")
    @patch("src.main.detect_partition")
    @patch("boto3.client")
    def test_opted_in_region_reaches_control_tower(
        self, mock_client, mock_partition, mock_parse_args, tmp_path
    ):
        """A landing zone in an opted-in region is found by the full run."""
        mock_parse_args.return_value = MagicMock(output=str(tmp_path))
        mock_partition.return_value = {
            "partition": "aws",
            "default_region": "us-east-1",
        }
        base_factory = _mock_management_account_boto3()

        def client_factory(service, **kwargs):
            client = base_factory(service, **kwargs)
            if service == "ec2":
                client.describe_regions.return_value = {
                    "Regions": [
                        {
                            "RegionName": "us-east-1",
                            "OptInStatus": "opt-in-not-required",
                        },
                        {"RegionName": "eu-south-1", "OptInStatus": "opted-in"},
                    ]
                }
            elif service == "controltower":
                landing_zones = (
                    [{"arn": "arn:aws:controltower:eu-south-1:111:landingzone/example"}]
                    if kwargs.get("region_name") == "eu-south-1"
                    else []
                )
                client.list_landing_zones.return_value = {"landingZones": landing_zones}
            return client

        mock_client.side_effect = client_factory

        checks, _ = run()

        control_tower = next(
            c for c in checks if c["check"] == "Control Tower deployed"
        )
        assert control_tower["status"] == "complete"
        mock_client.assert_any_call("controltower")
        mock_client.assert_any_call("controltower", region_name="eu-south-1")

    @patch("src.main.parse_args")
    @patch("src.checks.account_resources.boto3.client")
    @patch("src.checks.identity.boto3.client")
    @patch("src.checks.control_tower.boto3.client")
    @patch("src.checks.org_services.boto3.client")
    @patch("src.checks.organization.boto3.client")
    @patch("src.checks.org_map.boto3.client")
    @patch("src.checks.delegated_admin.boto3.client")
    @patch("src.discovery.regions.boto3.client")
    @patch("src.discovery.account_type.boto3.client")
    def test_all_checks_have_valid_schema(
        self,
        mock_acct_client,
        mock_regions_client,
        mock_da_client,
        mock_org_map_client,
        mock_org_client,
        mock_svc_client,
        mock_ct_client,
        mock_id_client,
        mock_res_client,
        mock_parse_args,
        tmp_path,
    ):
        """Every check result follows the Check Metadata Schema exactly."""
        mock_parse_args.return_value = MagicMock(output=str(tmp_path))

        factory = _mock_management_account_boto3()
        mock_acct_client.side_effect = factory
        mock_regions_client.side_effect = factory
        mock_da_client.side_effect = factory
        mock_org_map_client.side_effect = factory
        mock_org_client.side_effect = factory
        mock_svc_client.side_effect = factory
        mock_ct_client.side_effect = factory
        mock_id_client.side_effect = factory
        mock_res_client.side_effect = factory

        checks, _ = run()

        for c in checks:
            assert isinstance(c.get("check"), str), f"Missing 'check' string: {c}"
            assert isinstance(c.get("description"), str), f"Missing 'description': {c}"
            assert c.get("status") in ("complete", "incomplete", "error"), (
                f"Bad status: {c}"
            )
            assert isinstance(c.get("required"), bool), f"Missing 'required' bool: {c}"
            assert isinstance(c.get("weight"), int), f"Missing 'weight' int: {c}"
            assert isinstance(c.get("loe"), int), f"Missing 'loe' int: {c}"
            assert isinstance(c.get("remediationLink"), str), (
                f"Missing 'remediationLink': {c}"
            )
            assert c["remediationLink"].startswith("https://"), (
                f"Bad remediation URL: {c}"
            )
            if c["status"] == "error":
                assert "error" in c, f"Error status without 'error' field: {c}"

    @patch("src.main.parse_args")
    @patch("src.checks.account_resources.boto3.client")
    @patch("src.checks.identity.boto3.client")
    @patch("src.checks.control_tower.boto3.client")
    @patch("src.checks.org_services.boto3.client")
    @patch("src.checks.organization.boto3.client")
    @patch("src.checks.org_map.boto3.client")
    @patch("src.checks.delegated_admin.boto3.client")
    @patch("src.discovery.regions.boto3.client")
    @patch("src.discovery.account_type.boto3.client")
    def test_high_maturity_when_all_passing(
        self,
        mock_acct_client,
        mock_regions_client,
        mock_da_client,
        mock_org_map_client,
        mock_org_client,
        mock_svc_client,
        mock_ct_client,
        mock_id_client,
        mock_res_client,
        mock_parse_args,
        tmp_path,
    ):
        """When all checks pass, maturity should be Level 4 or 5."""
        mock_parse_args.return_value = MagicMock(output=str(tmp_path))

        factory = _mock_management_account_boto3()
        mock_acct_client.side_effect = factory
        mock_regions_client.side_effect = factory
        mock_da_client.side_effect = factory
        mock_org_map_client.side_effect = factory
        mock_org_client.side_effect = factory
        mock_svc_client.side_effect = factory
        mock_ct_client.side_effect = factory
        mock_id_client.side_effect = factory
        mock_res_client.side_effect = factory

        checks, maturity = run()

        # All our mocks return "passing" data, so maturity should be high
        assert maturity["level"] >= 4
        complete_count = sum(1 for c in checks if c["status"] == "complete")
        assert complete_count >= 30


# ============================================================================
# FR-12 — organization map orchestration
# ============================================================================


class TestOrganizationMapOrchestration:
    """How the organization map is gated and surfaced on failure."""

    @patch("src.main.parse_args")
    @patch("src.checks.account_resources.boto3.client")
    @patch("src.checks.identity.boto3.client")
    @patch("src.checks.control_tower.boto3.client")
    @patch("src.checks.org_services.boto3.client")
    @patch("src.checks.organization.boto3.client")
    @patch("src.checks.org_map.boto3.client")
    @patch("src.checks.delegated_admin.boto3.client")
    @patch("src.discovery.regions.boto3.client")
    @patch("src.discovery.account_type.boto3.client")
    def test_account_level_attachments_are_included(
        self,
        mock_acct_client,
        mock_regions_client,
        mock_da_client,
        mock_org_map_client,
        mock_org_client,
        mock_svc_client,
        mock_ct_client,
        mock_id_client,
        mock_res_client,
        mock_parse_args,
        tmp_path,
    ):
        """FR-12.3: account attachments need no opt-in with the inverted lookup."""
        mock_parse_args.return_value = MagicMock(output=str(tmp_path))
        factory = _mock_management_account_boto3()
        for mock_client in (
            mock_acct_client,
            mock_regions_client,
            mock_da_client,
            mock_org_map_client,
            mock_org_client,
            mock_svc_client,
            mock_ct_client,
            mock_id_client,
            mock_res_client,
        ):
            mock_client.side_effect = factory

        run()

        with open(os.path.join(str(tmp_path), "wafa-raw.json")) as f:
            raw = json.load(f)
        org_map = raw["organization_map"]

        def accounts(node):
            yield from node["accounts"]
            for child in node["children"]:
                yield from accounts(child)

        by_id = {a["id"]: a for a in accounts(org_map["root"])}
        # The workload account has an SCP attached directly to it.
        assert [p["name"] for p in by_id["444444444444"]["policies"]] == [
            "DenyRootUserActions"
        ]
        # An account with nothing attached still reports an empty list.
        assert by_id["111111111111"]["policies"] == []

    @patch("src.main.parse_args")
    @patch("src.checks.account_resources.boto3.client")
    @patch("src.checks.identity.boto3.client")
    @patch("src.discovery.regions.boto3.client")
    @patch("src.discovery.account_type.boto3.client")
    def test_limited_mode_skips_the_map_and_its_csv(
        self,
        mock_acct_client,
        mock_regions_client,
        mock_id_client,
        mock_res_client,
        mock_parse_args,
        tmp_path,
    ):
        """A member account produces no map rather than an errored one.

        Its map CSV still replaces an earlier run's, which would otherwise
        sit beside this run's reports and read as the current map.
        """
        output_dir = str(tmp_path)
        _write_stale_org_map_csv(output_dir)
        mock_parse_args.return_value = MagicMock(output=output_dir)
        factory = _mock_member_account_boto3()
        mock_acct_client.side_effect = factory
        mock_regions_client.side_effect = factory
        mock_id_client.side_effect = factory
        mock_res_client.side_effect = factory

        run()

        with open(os.path.join(output_dir, "wafa-raw.json")) as f:
            raw = json.load(f)
        assert raw["organization_map"] is None
        assert raw["organization_map_error"] is None
        rows = _read_org_map_csv(output_dir)
        assert len(rows) == 2
        assert rows[1][0] == "Not available"
        assert "only from the organization's management account" in rows[1][9]

    @patch("src.main.parse_args")
    @patch("src.main.get_organization_map")
    @patch("src.checks.account_resources.boto3.client")
    @patch("src.checks.identity.boto3.client")
    @patch("src.checks.control_tower.boto3.client")
    @patch("src.checks.org_services.boto3.client")
    @patch("src.checks.organization.boto3.client")
    @patch("src.checks.delegated_admin.boto3.client")
    @patch("src.discovery.regions.boto3.client")
    @patch("src.discovery.account_type.boto3.client")
    def test_map_failure_is_reported_without_blocking_the_assessment(
        self,
        mock_acct_client,
        mock_regions_client,
        mock_da_client,
        mock_org_client,
        mock_svc_client,
        mock_ct_client,
        mock_id_client,
        mock_res_client,
        mock_get_org_map,
        mock_parse_args,
        tmp_path,
    ):
        """A map failure surfaces in the report and in a replaced map CSV."""
        output_dir = str(tmp_path)
        _write_stale_org_map_csv(output_dir)
        mock_parse_args.return_value = MagicMock(output=output_dir)
        mock_get_org_map.return_value = (None, "AccessDeniedException")
        factory = _mock_management_account_boto3()
        for mock_client in (
            mock_acct_client,
            mock_regions_client,
            mock_da_client,
            mock_org_client,
            mock_svc_client,
            mock_ct_client,
            mock_id_client,
            mock_res_client,
        ):
            mock_client.side_effect = factory

        checks, _ = run()

        # The assessment itself still completed.
        assert len(checks) > 30
        with open(os.path.join(output_dir, "wafa-raw.json")) as f:
            raw = json.load(f)
        assert raw["organization_map"] is None
        assert raw["organization_map_error"] == "AccessDeniedException"
        with open(os.path.join(output_dir, "wafa-report.html")) as f:
            assert "AccessDeniedException" in f.read()
        rows = _read_org_map_csv(output_dir)
        assert len(rows) == 2
        assert rows[1][0] == "Not available"
        assert rows[1][9] == (
            "Organization map could not be built: AccessDeniedException"
        )

    @patch("src.main.parse_args")
    @patch("src.main.detect_partition")
    @patch("boto3.client")
    def test_s3_output_replaces_the_org_map_csv_without_a_map(
        self, mock_client, mock_partition, mock_parse_args
    ):
        """CodeBuild reuses one prefix per account, so the key is overwritten."""
        mock_parse_args.return_value = MagicMock(output="s3://results-bucket/reports")
        mock_partition.return_value = {
            "partition": "aws",
            "default_region": "us-east-1",
        }
        base_factory = _mock_member_account_boto3()
        mock_s3 = MagicMock()

        def client_factory(service, **kwargs):
            if service == "s3":
                return mock_s3
            return base_factory(service, **kwargs)

        mock_client.side_effect = client_factory

        run()

        uploaded = {
            call.kwargs["Key"].rsplit("/", 1)[-1]: call.kwargs
            for call in mock_s3.put_object.call_args_list
        }
        assert "Not available" in uploaded["wafa-org-map.csv"]["Body"]

    @patch("src.main.parse_args")
    @patch("src.main.detect_partition")
    @patch("boto3.client")
    def test_s3_output_uploads_the_org_map_csv(
        self, mock_client, mock_partition, mock_parse_args
    ):
        """CodeBuild writes to S3, so the map must be in the uploaded set."""
        mock_parse_args.return_value = MagicMock(output="s3://results-bucket/reports")
        mock_partition.return_value = {
            "partition": "aws",
            "default_region": "us-east-1",
        }
        base_factory = _mock_management_account_boto3()
        mock_s3 = MagicMock()

        def client_factory(service, **kwargs):
            if service == "s3":
                return mock_s3
            return base_factory(service, **kwargs)

        mock_client.side_effect = client_factory

        run()

        uploaded = {
            call.kwargs["Key"].rsplit("/", 1)[-1]: call.kwargs
            for call in mock_s3.put_object.call_args_list
        }
        assert "wafa-org-map.csv" in uploaded
        assert uploaded["wafa-org-map.csv"]["ContentType"] == "text/csv"
        assert "Type,OU Path,Name" in uploaded["wafa-org-map.csv"]["Body"]
        # The pre-existing reports are still uploaded alongside it.
        assert {"wafa-report.html", "wafa-checks.csv", "wafa-raw.json"} <= set(uploaded)

    @patch("src.main.parse_args")
    @patch("src.main.detect_partition")
    @patch("boto3.client")
    def test_map_cannot_change_checks_or_maturity(
        self, mock_client, mock_partition, mock_parse_args, tmp_path
    ):
        """FR-12 is informational: removing it must not move a single result."""
        mock_partition.return_value = {
            "partition": "aws",
            "default_region": "us-east-1",
        }

        def run_once(with_map):
            mock_parse_args.return_value = MagicMock(output=str(tmp_path))
            mock_client.side_effect = _mock_management_account_boto3()
            if with_map:
                return run()
            with patch("src.main.get_organization_map", return_value=(None, None)):
                return run()

        checks_with, maturity_with = run_once(True)
        checks_without, maturity_without = run_once(False)

        assert [c["check"] for c in checks_with] == [c["check"] for c in checks_without]
        assert [c["status"] for c in checks_with] == [
            c["status"] for c in checks_without
        ]
        assert maturity_with == maturity_without
        # No check is derived from the map.
        assert not [c for c in checks_with if "organization map" in c["check"].lower()]


# ============================================================================
# Member account — limited assessment
# ============================================================================


class TestRunMemberAccount:
    """End-to-end test: member account runs limited checks."""

    @patch("src.main.parse_args")
    @patch("src.checks.account_resources.boto3.client")
    @patch("src.checks.identity.boto3.client")
    @patch("src.discovery.regions.boto3.client")
    @patch("src.discovery.account_type.boto3.client")
    def test_limited_assessment(
        self,
        mock_acct_client,
        mock_regions_client,
        mock_id_client,
        mock_res_client,
        mock_parse_args,
        tmp_path,
    ):
        """Member account: only per-account checks run."""
        output_dir = str(tmp_path)
        mock_parse_args.return_value = MagicMock(output=output_dir)

        factory = _mock_member_account_boto3()
        mock_acct_client.side_effect = factory
        mock_regions_client.side_effect = factory
        mock_id_client.side_effect = factory
        mock_res_client.side_effect = factory

        checks, _maturity = run()

        # Limited: FR-3.1, FR-3.2, FR-6.2, FR-7.1-7.6 = 9 checks
        assert len(checks) == 9

        # FR-3.1 should be complete (org exists)
        org_check = next(c for c in checks if c["check"] == "AWS Organization exists")
        assert org_check["status"] == "complete"

        # FR-3.2 should be incomplete (not management)
        mgmt_check = next(
            c for c in checks if c["check"] == "Management account identified"
        )
        assert mgmt_check["status"] == "incomplete"

        # FR-6.2: IAM users exist → incomplete
        iam_check = next(c for c in checks if c["check"] == "No IAM users in account")
        assert iam_check["status"] == "incomplete"

        # FR-7.6: VPCs exist → incomplete
        vpc_check = next(c for c in checks if c["check"] == "No VPCs in account")
        assert vpc_check["status"] == "incomplete"

        # Reports still generated
        assert os.path.exists(os.path.join(output_dir, "wafa-report.html"))
        assert os.path.exists(os.path.join(output_dir, "wafa-raw.json"))

    @patch("src.main.parse_args")
    @patch("src.checks.account_resources.boto3.client")
    @patch("src.checks.identity.boto3.client")
    @patch("src.discovery.regions.boto3.client")
    @patch("src.discovery.account_type.boto3.client")
    def test_member_maturity_is_low(
        self,
        mock_acct_client,
        mock_regions_client,
        mock_id_client,
        mock_res_client,
        mock_parse_args,
        tmp_path,
    ):
        """Member account can't reach high maturity (missing management-only checks)."""
        mock_parse_args.return_value = MagicMock(output=str(tmp_path))

        factory = _mock_member_account_boto3()
        mock_acct_client.side_effect = factory
        mock_regions_client.side_effect = factory
        mock_id_client.side_effect = factory
        mock_res_client.side_effect = factory

        _, maturity = run()

        # Without management account checks, can't exceed Level 1
        assert maturity["level"] == 1


# ============================================================================
# Standalone account — limited assessment
# ============================================================================


class TestRunStandaloneAccount:
    """End-to-end test: standalone account (not in an org) runs limited checks."""

    @patch("src.main.parse_args")
    @patch("src.checks.account_resources.boto3.client")
    @patch("src.checks.identity.boto3.client")
    @patch("src.discovery.regions.boto3.client")
    @patch("src.discovery.account_type.boto3.client")
    def test_limited_assessment(
        self,
        mock_acct_client,
        mock_regions_client,
        mock_id_client,
        mock_res_client,
        mock_parse_args,
        tmp_path,
    ):
        """Standalone account: same limited check set as a member account (C3)."""
        output_dir = str(tmp_path)
        mock_parse_args.return_value = MagicMock(output=output_dir)

        factory = _mock_standalone_account_boto3()
        mock_acct_client.side_effect = factory
        mock_regions_client.side_effect = factory
        mock_id_client.side_effect = factory
        mock_res_client.side_effect = factory

        checks, maturity = run()

        # Limited: FR-3.1, FR-3.2, FR-6.2, FR-7.1-7.6 = 9 checks
        assert len(checks) == 9
        assert maturity["level"] == 1

        # Not in an org → both always-run org checks are 'incomplete'.
        # check_org_exists maps AWSOrganizationsNotInUseException to 'incomplete'
        # (a standalone account legitimately has no org — a gap, not an error).
        org_check = next(c for c in checks if c["check"] == "AWS Organization exists")
        assert org_check["status"] == "incomplete"
        mgmt_check = next(
            c for c in checks if c["check"] == "Management account identified"
        )
        assert mgmt_check["status"] == "incomplete"

        # Reports still generated
        assert os.path.exists(os.path.join(output_dir, "wafa-report.html"))
        assert os.path.exists(os.path.join(output_dir, "wafa-raw.json"))

        # FR-12: standalone is a limited mode, so the map is absent rather
        # than reported as a failure, and no map CSV is written.
        with open(os.path.join(output_dir, "wafa-raw.json")) as f:
            raw = json.load(f)
        assert raw["organization_map"] is None
        assert raw["organization_map_error"] is None
        rows = _read_org_map_csv(output_dir)
        assert len(rows) == 2
        assert rows[1][0] == "Not available"
        assert "only from the organization's management account" in rows[1][9]

    @patch("src.main.parse_args")
    @patch("src.checks.account_resources.boto3.client")
    @patch("src.checks.identity.boto3.client")
    @patch("src.discovery.regions.boto3.client")
    @patch("src.discovery.account_type.boto3.client")
    def test_standalone_maturity_is_low(
        self,
        mock_acct_client,
        mock_regions_client,
        mock_id_client,
        mock_res_client,
        mock_parse_args,
        tmp_path,
    ):
        """Standalone account is pinned at maturity Level 1."""
        mock_parse_args.return_value = MagicMock(output=str(tmp_path))

        factory = _mock_standalone_account_boto3()
        mock_acct_client.side_effect = factory
        mock_regions_client.side_effect = factory
        mock_id_client.side_effect = factory
        mock_res_client.side_effect = factory

        _, maturity = run()

        assert maturity["level"] == 1


class TestRunUnknownAccountType:
    """DescribeOrganization denied: run limited checks instead of crashing."""

    @patch("src.main.parse_args")
    @patch("src.checks.organization.boto3.client")
    @patch("src.checks.account_resources.boto3.client")
    @patch("src.checks.identity.boto3.client")
    @patch("src.discovery.regions.boto3.client")
    @patch("src.discovery.account_type.boto3.client")
    def test_access_denied_runs_limited_assessment(
        self,
        mock_acct_client,
        mock_regions_client,
        mock_id_client,
        mock_res_client,
        mock_org_client,
        mock_parse_args,
        tmp_path,
    ):
        from botocore.exceptions import ClientError

        mock_parse_args.return_value = MagicMock(output=str(tmp_path))
        standalone = _mock_standalone_account_boto3()

        def factory(service, **kwargs):
            if service == "organizations":
                mock = MagicMock()
                mock.describe_organization.side_effect = ClientError(
                    {"Error": {"Code": "AccessDeniedException", "Message": "Denied"}},
                    "DescribeOrganization",
                )
                return mock
            return standalone(service, **kwargs)

        for mock_client in (
            mock_acct_client,
            mock_regions_client,
            mock_id_client,
            mock_res_client,
            mock_org_client,
        ):
            mock_client.side_effect = factory

        checks, _ = run()

        assert len(checks) == 9
        statuses = {c["check"]: c["status"] for c in checks}
        assert statuses["AWS Organization exists"] == "error"
        assert statuses["Management account identified"] == "error"
        raw = json.loads((tmp_path / "wafa-raw.json").read_text())
        assert raw["account_info"]["account_type"] == "unknown"
        assert "AccessDeniedException" in raw["account_info"]["detection_error"]
        assert raw["delegated_administrators_error"] is None

        # FR-12: an undetermined account type takes the limited path, so the
        # map is absent rather than reported as a failure.
        assert raw["organization_map"] is None
        assert raw["organization_map_error"] is None
        rows = _read_org_map_csv(str(tmp_path))
        assert rows[1][0] == "Not available"
        assert "account type could not be determined" in rows[1][9]


# ============================================================================
# S3 upload path
# ============================================================================


class TestUploadToS3:
    """Test the S3 upload logic."""

    @patch("src.main.boto3.client")
    def test_upload_to_s3(self, mock_client):
        """Files are uploaded with correct keys."""
        mock_s3 = MagicMock()
        mock_client.return_value = mock_s3

        upload_to_s3(
            "s3://my-bucket/reports/",
            "111111111111",
            [
                ("wafa-report.html", "<html>test</html>", "text/html"),
                ("wafa-raw.json", '{"test": true}', "application/json"),
            ],
        )

        assert mock_s3.put_object.call_count == 2
        calls = mock_s3.put_object.call_args_list

        # First call: HTML report
        assert calls[0].kwargs["Bucket"] == "my-bucket"
        assert calls[0].kwargs["Key"] == "reports/111111111111/wafa-report.html"
        assert calls[0].kwargs["ContentType"] == "text/html"

        # Second call: JSON
        assert calls[1].kwargs["Key"] == "reports/111111111111/wafa-raw.json"

    @patch("src.main.boto3.client")
    def test_upload_to_s3_no_prefix(self, mock_client):
        """S3 upload works when URI has no prefix (just bucket)."""
        mock_s3 = MagicMock()
        mock_client.return_value = mock_s3

        upload_to_s3(
            "s3://my-bucket",
            "123456789012",
            [("wafa-report.html", "test", "text/html")],
        )

        call = mock_s3.put_object.call_args
        assert call.kwargs["Bucket"] == "my-bucket"
        assert call.kwargs["Key"] == "123456789012/wafa-report.html"


# ============================================================================
# print_summary
# ============================================================================


class TestPrintSummary:
    """Test console output formatting."""

    def test_prints_without_error(self, capsys):
        """print_summary doesn't crash and outputs expected content."""
        checks = [
            {
                "check": "Test Check",
                "description": "A test",
                "status": "complete",
                "required": True,
                "weight": 6,
                "loe": 1,
                "remediationLink": "https://example.com",
            },
            {
                "check": "Another Check",
                "description": "Another test",
                "status": "incomplete",
                "required": False,
                "weight": 4,
                "loe": 2,
                "remediationLink": "https://example.com",
            },
        ]
        maturity = {
            "level": 2,
            "name": "Established",
            "description": "Org in place.",
            "next_level": 3,
            "next_level_progress": 50.0,
            "next_level_checks_needed": ["SCP enabled"],
        }

        print_summary(checks, maturity)
        captured = capsys.readouterr()

        assert "WAFA ASSESSMENT RESULTS" in captured.out
        assert "Level 2" in captured.out or "Maturity Level: 2" in captured.out
        assert "Test Check" in captured.out
        assert "complete" in captured.out
        assert "incomplete" in captured.out
        assert "50%" in captured.out

    def test_prints_level_5_no_next_steps(self, capsys):
        """Level 5: no next level shown."""
        checks = [
            {
                "check": "All Done",
                "description": "Done",
                "status": "complete",
                "required": True,
                "weight": 6,
                "loe": 1,
                "remediationLink": "https://example.com",
            },
        ]
        maturity = {
            "level": 5,
            "name": "Expert",
            "description": "Fully codified.",
            "next_level": None,
            "next_level_progress": 100,
            "next_level_checks_needed": [],
        }

        print_summary(checks, maturity)
        captured = capsys.readouterr()

        assert "Expert" in captured.out
        assert "Progress toward" not in captured.out


def test_exit_code_fails_only_when_every_check_errors():
    assert exit_code([{"status": "error"}, {"status": "error"}]) == 1
    assert exit_code([{"status": "error"}, {"status": "incomplete"}]) == 0
    assert exit_code([{"status": "complete"}]) == 0
    assert exit_code([]) == 0


# ============================================================================
# FR-12.5 — organization map console summary
# ============================================================================


STALE_ACCOUNT_ID = "999999999999"


def _write_stale_org_map_csv(output_dir):
    """Leave a map CSV from an earlier run in the output directory."""
    with open(os.path.join(output_dir, "wafa-org-map.csv"), "w") as f:
        f.write(f"Type,OU Path,Name,ID\r\nAccount,Root,Old,{STALE_ACCOUNT_ID}\r\n")


def _read_org_map_csv(output_dir):
    """Parse the map CSV and assert none of the earlier run's data remains."""
    with open(os.path.join(output_dir, "wafa-org-map.csv"), newline="") as f:
        content = f.read()
    assert STALE_ACCOUNT_ID not in content
    return list(csv.reader(io.StringIO(content)))


def _summary_map(**overrides):
    """A minimal organization map for console summary tests."""
    data = {
        "organizationId": "o-example12345",
        "enabledPolicyTypes": ["SERVICE_CONTROL_POLICY"],
        "policyTypesNotEnabled": ["RESOURCE_CONTROL_POLICY"],
        "policyLookupErrors": [],
        "totals": {"organizationalUnits": 1, "accounts": 1, "maxDepth": 1},
        "root": {
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
            "accounts": [
                {
                    "id": "111111111111",
                    "name": "Management",
                    "status": "ACTIVE",
                    "policies": [],
                }
            ],
            "children": [],
        },
    }
    data.update(overrides)
    return data


def _print_summary(data, error=None):
    """Print the console summary with the rows the orchestrator would pass.

    ``run()`` flattens the map once and hands the same rows to the console,
    the CSV, and the HTML report, so the tests exercise it the same way.
    """
    print_org_map_summary(data, error, flatten_rows(data))


class TestPrintOrgMapSummary:
    def test_prints_totals_and_attachment_count(self, capsys):
        _print_summary(_summary_map())
        out = capsys.readouterr().out
        assert "1 OUs" in out
        assert "1 accounts" in out
        assert "1 direct policy attachments" in out

    def test_reports_policy_types_not_enabled(self, capsys):
        """The console uses the same display labels as the CSV and the HTML."""
        _print_summary(_summary_map())
        out = capsys.readouterr().out
        assert "Policy types not enabled: RCP" in out
        assert "RESOURCE_CONTROL_POLICY" not in out

    def test_reports_policy_lookup_errors(self, capsys):
        data = _summary_map(
            policyLookupErrors=[
                {
                    "scope": "INSPECTOR_POLICY: ScanAll",
                    "policyType": "INSPECTOR_POLICY",
                    "policyName": "ScanAll",
                    "error": "TooManyRequestsException",
                }
            ]
        )
        _print_summary(data)
        out = capsys.readouterr().out
        assert "may be incomplete for Inspector: ScanAll" in out
        # The same label as the policy badges, never the API constant.
        assert "INSPECTOR_POLICY" not in out
        assert "TooManyRequestsException" in out

    def test_reports_pending_policy_types(self, capsys):
        data = _summary_map(
            policyTypesPending=[
                {"type": "SERVICE_CONTROL_POLICY", "status": "PENDING_ENABLE"}
            ]
        )
        _print_summary(data)
        out = capsys.readouterr().out
        assert "SCP is changing state (PENDING_ENABLE)" in out
        assert "attachments are not mapped" in out

    def test_reports_a_denied_account_listing(self, capsys):
        """Without this the totals read as a complete, smaller organization."""
        data = _summary_map()
        data["root"]["accountsError"] = "AccessDeniedException"
        _print_summary(data)
        out = capsys.readouterr().out
        assert "AccessDeniedException" in out
        assert "Root" in out

    def test_reports_an_unreadable_child_ou(self, capsys):
        data = _summary_map()
        data["root"]["childrenError"] = "ThrottlingException"
        _print_summary(data)
        assert "ThrottlingException" in capsys.readouterr().out

    def test_reports_a_truncated_walk(self, capsys):
        data = _summary_map()
        data["root"]["childrenTruncated"] = "Walk stopped at the 5-level limit"
        _print_summary(data)
        assert "Walk stopped" in capsys.readouterr().out

    def test_states_that_the_counts_are_lower_bounds(self, capsys):
        """The totals count what was read, so say when a node was not read."""
        data = _summary_map(
            nodeListingErrors=True,
            nodeListingErrorPaths=["Root/Security"],
            depthTruncatedPaths=["Root/Deep"],
        )
        _print_summary(data)
        out = capsys.readouterr().out

        assert "Map is INCOMPLETE: 2 node(s)" in out
        assert "lower bounds" in out

    def test_counts_a_node_in_both_lists_once(self, capsys):
        """A node whose listing failed and whose walk stopped is one node."""
        data = _summary_map(
            nodeListingErrorPaths=["Root/A/B/C/D/Deep"],
            depthTruncatedPaths=["Root/A/B/C/D/Deep"],
        )
        _print_summary(data)
        assert "Map is INCOMPLETE: 1 node(s)" in capsys.readouterr().out

    def test_a_complete_map_is_not_called_incomplete(self, capsys):
        _print_summary(_summary_map())
        assert "INCOMPLETE" not in capsys.readouterr().out

    def test_caps_warning_lines_and_states_the_withheld_count(self, capsys):
        """A throttled large organization must not bury the check results."""
        failures = [
            {"scope": f"TAG_POLICY: policy-{index}", "error": "TooManyRequests"}
            for index in range(MAX_ORG_MAP_CONSOLE_WARNINGS + 7)
        ]
        _print_summary(_summary_map(policyLookupErrors=failures))
        out = capsys.readouterr().out

        printed = [line for line in out.splitlines() if "may be incomplete" in line]
        assert len(printed) == MAX_ORG_MAP_CONSOLE_WARNINGS
        assert "and 7 more organization map warning(s)" in out
        assert "wafa-org-map.csv" in out

    def test_prints_every_warning_when_under_the_cap(self, capsys):
        data = _summary_map(
            policyLookupErrors=[{"scope": "TAG_POLICY", "error": "Denied"}]
        )
        data["root"]["accountsError"] = "AccessDeniedException"
        _print_summary(data)
        out = capsys.readouterr().out

        assert "TAG_POLICY" in out
        assert "AccessDeniedException" in out
        assert "more organization map warning" not in out

    def test_reports_a_map_that_could_not_be_built(self, capsys):
        print_org_map_summary(None, "AccessDeniedException", [])
        out = capsys.readouterr().out
        assert "could not be built" in out
        assert "AccessDeniedException" in out

    def test_prints_nothing_without_a_map(self, capsys):
        print_org_map_summary(None, None, [])
        assert capsys.readouterr().out == ""
