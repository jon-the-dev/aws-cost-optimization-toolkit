"""Synthetic AWS API responses used by the tests and by generate_samples.py.

Every identifier here is invented. Account 111122223333 is one of the reserved example
account IDs AWS uses in its own documentation.
"""

from __future__ import annotations

ACCOUNT = "111122223333"
REGION = "us-east-1"

ENROLLMENT_ACTIVE = {"status": "Active", "memberAccountsEnrolled": False}
ENROLLMENT_INACTIVE = {"status": "Inactive", "statusReason": "Account has not opted in"}


def _savings(value: float, pct: float) -> dict:
    return {
        "savingsOpportunityPercentage": pct,
        "estimatedMonthlySavings": {"currency": "USD", "value": value},
    }


EC2_RECOMMENDATIONS = {
    "instanceRecommendations": [
        {
            "instanceArn": f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/i-0aa11bb22cc33dd44",
            "accountId": ACCOUNT,
            "instanceName": "reporting-worker-1",
            "currentInstanceType": "m5.4xlarge",
            "finding": "Overprovisioned",
            "recommendationOptions": [
                {
                    "instanceType": "m6i.2xlarge",
                    "rank": 1,
                    "savingsOpportunity": _savings(283.24, 41.3),
                },
                {
                    "instanceType": "m6i.xlarge",
                    "rank": 2,
                    "savingsOpportunity": _savings(421.86, 61.5),
                },
            ],
        },
        {
            "instanceArn": f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/i-055ee66ff77aa88bb",
            "accountId": ACCOUNT,
            "instanceName": "api-green-2",
            "currentInstanceType": "c5.xlarge",
            "finding": "Optimized",
            "recommendationOptions": [
                {"instanceType": "c5.xlarge", "rank": 1, "savingsOpportunity": _savings(0.0, 0.0)},
            ],
        },
        {
            "instanceArn": f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/i-099cc00dd11ee22ff",
            "accountId": ACCOUNT,
            "instanceName": "batch-transcode",
            "currentInstanceType": "r5.2xlarge",
            "finding": "Underprovisioned",
            "recommendationOptions": [
                {
                    "instanceType": "r6i.4xlarge",
                    "rank": 1,
                    "savingsOpportunity": _savings(0.0, 0.0),
                },
            ],
        },
    ]
}

EBS_RECOMMENDATIONS = {
    "volumeRecommendations": [
        {
            "volumeArn": f"arn:aws:ec2:{REGION}:{ACCOUNT}:volume/vol-0123456789abcdef0",
            "accountId": ACCOUNT,
            "currentConfiguration": {
                "volumeType": "io1",
                "volumeSize": 500,
                "volumeBaselineIOPS": 10000,
            },
            "finding": "NotOptimized",
            "volumeRecommendationOptions": [
                {
                    "configuration": {
                        "volumeType": "gp3",
                        "volumeSize": 500,
                        "volumeBaselineIOPS": 3000,
                    },
                    "rank": 1,
                    "savingsOpportunity": _savings(597.5, 78.0),
                }
            ],
        },
        {
            "volumeArn": f"arn:aws:ec2:{REGION}:{ACCOUNT}:volume/vol-0fedcba987654321f",
            "accountId": ACCOUNT,
            "currentConfiguration": {"volumeType": "gp3", "volumeSize": 100},
            "finding": "Optimized",
            "volumeRecommendationOptions": [
                {
                    "configuration": {"volumeType": "gp3", "volumeSize": 100},
                    "rank": 1,
                    "savingsOpportunity": _savings(0.0, 0.0),
                }
            ],
        },
    ]
}

LAMBDA_RECOMMENDATIONS = {
    "lambdaFunctionRecommendations": [
        {
            "functionArn": f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:image-thumbnailer",
            "accountId": ACCOUNT,
            "functionVersion": "$LATEST",
            "currentMemorySize": 3008,
            "finding": "NotOptimized",
            "memorySizeRecommendationOptions": [
                {"memorySize": 1024, "rank": 1, "savingsOpportunity": _savings(64.18, 52.7)},
            ],
        },
        {
            "functionArn": f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:webhook-router",
            "accountId": ACCOUNT,
            "functionVersion": "$LATEST",
            "currentMemorySize": 128,
            "finding": "Unavailable",
            "memorySizeRecommendationOptions": [],
        },
    ]
}

SP_UTILIZATION = {
    "SavingsPlansUtilizationsByTime": [],
    "Total": {
        "Utilization": {
            "TotalCommitment": "7200.00",
            "UsedCommitment": "6660.72",
            "UnusedCommitment": "539.28",
            "UtilizationPercentage": "92.51",
        },
        "Savings": {"NetSavings": "2184.36", "OnDemandCostEquivalent": "8845.08"},
    },
}

SP_COVERAGE = {
    "SavingsPlansCoverages": [
        {
            "Attributes": {},
            "Coverage": {
                "SpendCoveredBySavingsPlans": "6660.72",
                "OnDemandCost": "3120.44",
                "TotalCost": "9781.16",
                "CoveragePercentage": "68.10",
            },
            "TimePeriod": {"Start": "2026-07-09", "End": "2026-08-08"},
        }
    ]
}

SP_PURCHASE_RECOMMENDATION = {
    "SavingsPlansPurchaseRecommendation": {
        "AccountScope": "PAYER",
        "SavingsPlansType": "COMPUTE_SP",
        "TermInYears": "ONE_YEAR",
        "PaymentOption": "NO_UPFRONT",
        "SavingsPlansPurchaseRecommendationSummary": {
            "EstimatedROI": "27.94",
            "CurrencyCode": "USD",
            "CurrentOnDemandSpend": "3120.44",
            "EstimatedSavingsAmount": "681.72",
            "EstimatedMonthlySavingsAmount": "681.72",
            "EstimatedSavingsPercentage": "21.85",
            "HourlyCommitmentToPurchase": "3.34",
            "DailyCommitmentToPurchase": "80.16",
            "TotalRecommendationCount": "1",
        },
    }
}

DESCRIBE_INSTANCES = {
    "Reservations": [
        {
            "Instances": [
                {
                    "InstanceId": "i-0aa11bb22cc33dd44",
                    "State": {"Name": "running"},
                    "Tags": [
                        {"Key": "Name", "Value": "dev-api-1"},
                        {"Key": "Schedule", "Value": "office-hours"},
                    ],
                },
                {
                    "InstanceId": "i-055ee66ff77aa88bb",
                    "State": {"Name": "stopped"},
                    "Tags": [
                        {"Key": "Name", "Value": "dev-worker-1"},
                        {"Key": "Schedule", "Value": "office-hours"},
                    ],
                },
            ]
        },
        {
            "Instances": [
                {
                    "InstanceId": "i-099cc00dd11ee22ff",
                    "State": {"Name": "running"},
                    "Tags": [{"Key": "Schedule", "Value": "office-hours"}],
                }
            ]
        },
    ]
}
