from __future__ import annotations

import fixtures
import pytest
import rightsizing_report as report
from botocore.exceptions import ClientError


def _stub_all(stubber):
    stubber.add_response("get_ec2_instance_recommendations", fixtures.EC2_RECOMMENDATIONS, {})
    stubber.add_response("get_ebs_volume_recommendations", fixtures.EBS_RECOMMENDATIONS, {})
    stubber.add_response("get_lambda_function_recommendations", fixtures.LAMBDA_RECOMMENDATIONS, {})


def test_report_picks_the_largest_saving_option_and_sorts_by_savings(stubbed):
    client, stubber = stubbed("compute-optimizer")
    _stub_all(stubber)

    rows = report.build_report(
        client, ["ec2", "ebs", "lambda"], min_savings=0.0, include_optimized=False
    )

    assert [r["resource_id"] for r in rows] == [
        "vol-0123456789abcdef0",
        "reporting-worker-1",
        "image-thumbnailer",
        "batch-transcode",
    ]
    # m6i.xlarge saves more than the rank-1 m6i.2xlarge, so it must win.
    assert rows[1]["recommended"] == "m6i.xlarge"
    assert rows[1]["monthly_savings"] == 421.86


def test_optimized_resources_are_excluded_by_default(stubbed):
    client, stubber = stubbed("compute-optimizer")
    _stub_all(stubber)

    rows = report.build_report(
        client, ["ec2", "ebs", "lambda"], min_savings=0.0, include_optimized=False
    )

    assert "api-green-2" not in [r["resource_id"] for r in rows]
    assert "vol-0fedcba987654321f" not in [r["resource_id"] for r in rows]


def test_include_optimized_keeps_them(stubbed):
    client, stubber = stubbed("compute-optimizer")
    _stub_all(stubber)

    rows = report.build_report(
        client, ["ec2", "ebs", "lambda"], min_savings=0.0, include_optimized=True
    )

    assert "api-green-2" in [r["resource_id"] for r in rows]


def test_min_savings_filters_low_value_rows(stubbed):
    client, stubber = stubbed("compute-optimizer")
    _stub_all(stubber)

    rows = report.build_report(
        client, ["ec2", "ebs", "lambda"], min_savings=100.0, include_optimized=False
    )

    assert [r["resource_id"] for r in rows] == ["vol-0123456789abcdef0", "reporting-worker-1"]


def test_recommendations_without_options_are_skipped(stubbed):
    client, stubber = stubbed("compute-optimizer")
    stubber.add_response("get_lambda_function_recommendations", fixtures.LAMBDA_RECOMMENDATIONS, {})

    rows = report.build_report(client, ["lambda"], min_savings=0.0, include_optimized=True)

    assert [r["resource_id"] for r in rows] == ["image-thumbnailer"]


def test_next_token_is_followed(stubbed):
    client, stubber = stubbed("compute-optimizer")
    first_page = {**fixtures.LAMBDA_RECOMMENDATIONS, "nextToken": "page-2"}
    stubber.add_response("get_lambda_function_recommendations", first_page, {})
    stubber.add_response(
        "get_lambda_function_recommendations",
        fixtures.LAMBDA_RECOMMENDATIONS,
        {"nextToken": "page-2"},
    )

    rows = report.build_report(client, ["lambda"], min_savings=0.0, include_optimized=True)

    assert len(rows) == 2
    stubber.assert_no_pending_responses()


def test_empty_result_set_is_not_an_error(stubbed):
    client, stubber = stubbed("compute-optimizer")
    stubber.add_response("get_ec2_instance_recommendations", {"instanceRecommendations": []}, {})

    assert report.build_report(client, ["ec2"], min_savings=0.0, include_optimized=False) == []


def test_inactive_enrollment_exits_with_an_actionable_message(stubbed, capsys):
    client, stubber = stubbed("compute-optimizer")
    stubber.add_response("get_enrollment_status", fixtures.ENROLLMENT_INACTIVE, {})

    with pytest.raises(SystemExit) as excinfo:
        report.check_enrollment(client)

    assert excinfo.value.code == 1
    stderr = capsys.readouterr().err
    assert "Inactive" in stderr
    assert "update-enrollment-status --status Active" in stderr


def test_active_enrollment_passes(stubbed):
    client, stubber = stubbed("compute-optimizer")
    stubber.add_response("get_enrollment_status", fixtures.ENROLLMENT_ACTIVE, {})

    report.check_enrollment(client)


def test_access_denied_propagates_rather_than_reporting_zero_savings(stubbed):
    client, stubber = stubbed("compute-optimizer")
    stubber.add_client_error(
        "get_ec2_instance_recommendations", service_error_code="AccessDeniedException"
    )

    with pytest.raises(ClientError):
        report.build_report(client, ["ec2"], min_savings=0.0, include_optimized=False)
