# aws-cost-optimization-toolkit

Runnable scripts and templates for the AWS cost levers that pay off first: rightsizing,
non-production schedules, budget alerting, Savings Plans coverage, and Cost and Usage Report
analysis.

Companion repository for [Daily DevOps](https://www.daily-devops.com/).

## Who This Is For

Engineers and platform teams who own an AWS bill and want to act on it this week without
buying a FinOps platform. Everything here runs from a laptop or a scheduled job with
nothing but Python and an AWS credential.

## What Is Included

The repository has two tracks. The **live-API** scripts query AWS directly and need only a
credential. The **CUR** scripts read a Cost and Usage Report export from local disk, which
is the only way to get resource-level and tag-level detail that Cost Explorer aggregates
away.

### Live API

| Artifact | What it does | Writes to AWS? |
| --- | --- | --- |
| [`scripts/rightsizing_report.py`](scripts/rightsizing_report.py) | Pulls Compute Optimizer EC2, EBS, and Lambda recommendations and ranks them by estimated monthly savings | No |
| [`scripts/savings_coverage_report.py`](scripts/savings_coverage_report.py) | Reports Savings Plans utilization, coverage, and the current purchase recommendation | No |
| [`scripts/instance_scheduler.py`](scripts/instance_scheduler.py) | Starts and stops tag-selected EC2 instances on an office-hours schedule | Only with `--apply` |
| [`templates/budget-alerts.yaml`](templates/budget-alerts.yaml) | CloudFormation: monthly budget with 50/80/100% and forecast alerts, plus a cost anomaly monitor, both on one SNS topic | Yes, on deploy |

### Cost and Usage Report

| Artifact | What it does | Writes to AWS? |
| --- | --- | --- |
| [`scripts/tag_health_check.py`](scripts/tag_health_check.py) | Detects cost-allocation tag drift: two CUR columns feeding one canonical tag, tag columns that vanish between months, and non-canonical keys | No |
| [`scripts/cur_untagged_drill.py`](scripts/cur_untagged_drill.py) | Splits untagged spend into resources you can actually tag and account-level charges you cannot | No |
| [`scripts/cur_resource_diff.py`](scripts/cur_resource_diff.py) | Diffs billed resources between two months: new, disappeared, and materially moved | No |

`tag_health_check.py` is the one to run first. A renamed tag key produces two CUR columns
that map to the same canonical tag, spend silently splits across both, and every report
downstream keeps rendering while quietly using only one of them. Nothing else in the
toolkit will tell you that is happening.

Sample output for the live-API scripts is committed under
[`examples/output/`](examples/output/). The CUR scripts write self-contained HTML reports;
their output is account-specific, so it is not committed.

## Prerequisites

- Python 3.11+
- An AWS credential (named profile, environment variables, or an instance/task role)
- Compute Optimizer enrolled in the account, for the rightsizing report:
  `aws compute-optimizer update-enrollment-status --status Active`
- Cost Explorer enabled, for the savings report. Note that Cost Explorer charges about
  **0.01 USD per API request**, so each run of that script costs a few cents.
- A CUR export synced locally, for the three CUR scripts. See below.

Minimum IAM for the read-only reports is in
[`docs/iam-readonly-policy.json`](docs/iam-readonly-policy.json). The scheduler needs the
tag-scoped policy in [`docs/iam-scheduler-policy.json`](docs/iam-scheduler-policy.json),
which only permits start/stop on instances carrying the schedule tag.

### Getting CUR Data

Create a Cost and Usage Report in the Billing console with **Parquet** format and hourly
granularity, pointed at an S3 bucket you own. AWS delivers the first file within about 24
hours and refreshes it several times a day. The export itself is free; you pay only S3
storage, which is cents a month for most accounts.

Then sync it down:

```bash
export CUR_BUCKET=my-cur-bucket
export CUR_PREFIX=cur
aws s3 sync "s3://$CUR_BUCKET/$CUR_PREFIX/" cur-data/
```

The scripts read `cur-data/` by default; use `--cur-dir` to point elsewhere. They expect
the Hive-style partitions AWS emits (`year=2026/month=7/`), because
`tag_health_check.py` derives the reporting month from the file path.

Cost-allocation tags only appear as CUR columns after you activate them under **Billing →
Cost allocation tags**, and activation is not retroactive. The canonical tag names the
scripts look for are defined in [`scripts/cur_common.py`](scripts/cur_common.py); override
them with `--tag-aliases` pointing at a JSON file:

```json
{
  "_comment": "Keys starting with an underscore are ignored, so the file can carry a note.",
  "Company": ["company", "customer"],
  "CostCenter": ["costcenter", "cost_center"],
  "Team": ["team", "squad"]
}
```

Supplying `--tag-aliases` replaces the defaults rather than merging with them, so list
every tag you want resolved. A tag missing from the map is silently unresolvable, which
is the failure `tag_health_check.py` is there to catch.

## Quickstart

```bash
git clone https://github.com/jon-the-dev/aws-cost-optimization-toolkit.git
cd aws-cost-optimization-toolkit
pipenv install --dev        # or: pip install boto3

# Where is the money? Largest rightsizing wins first.
pipenv run python scripts/rightsizing_report.py --region us-east-1 --min-savings 25

# What is still on-demand, and what would a Savings Plan cover?
pipenv run python scripts/savings_coverage_report.py --days 30

# Tag the non-prod instances you want on a schedule, then preview the plan.
aws ec2 create-tags --resources i-0123456789abcdef0 --tags Key=Schedule,Value=office-hours
pipenv run python scripts/instance_scheduler.py \
    --schedule "mon-fri 08:00-18:00" --timezone America/New_York

# Happy with the plan? Enforce it.
pipenv run python scripts/instance_scheduler.py \
    --schedule "mon-fri 08:00-18:00" --timezone America/New_York --apply
```

Deploy the budget and anomaly alerts:

```bash
aws cloudformation deploy \
    --template-file templates/budget-alerts.yaml \
    --stack-name cost-alerts \
    --parameter-overrides \
        MonthlyBudgetAmount=1000 \
        NotificationEmail=you@example.com \
        EnvironmentTag=prod
```

Confirm the SNS subscription email afterwards. Until that link is clicked, the budget fires
but nothing is delivered.

Every live-API script takes `--output table|markdown|csv|json`, so the reports drop straight
into a ticket, a spreadsheet, or a downstream job.

Then work the CUR, once an export is synced:

```bash
# Is cost attribution even trustworthy? Run this before believing any tag report.
pipenv run python scripts/tag_health_check.py --output reports/tag-health.html

# Of the untagged spend, how much can actually be fixed?
pipenv run python scripts/cur_untagged_drill.py --tag Application --top 25

# What showed up on the bill this month that was not there last month?
pipenv run python scripts/cur_resource_diff.py --min-cost 1.00
```

The CUR scripts write a self-contained HTML report alongside their console summary, and
take `--json` to dump raw findings for a downstream job. `tag_health_check.py` also takes
`--strict`, which exits non-zero when it finds a collision or a disappeared column, so it
can gate a reporting pipeline.

## Running The Scheduler On A Timer

The scheduler is a plain script with no server component, so anything that can run a command
on a timer will do. Two invocations a day are enough:

```cron
# crontab on a host with an AWS credential, times in the host's timezone
0 8  * * 1-5  cd /opt/aws-cost-optimization-toolkit && python scripts/instance_scheduler.py --force start --apply
0 18 * * 1-5  cd /opt/aws-cost-optimization-toolkit && python scripts/instance_scheduler.py --force stop  --apply
```

`--force` skips the schedule calculation entirely, which is what you want when the cron
expression already encodes the calendar. Use `--schedule`/`--timezone` instead when the
script runs on a fixed interval and has to decide for itself.

## Safety Notes

- **The scheduler is dry-run by default.** It prints the plan and changes nothing until you
  pass `--apply`. Read the plan before you pass it.
- **Stopping an instance is not free of consequences.** Instance store volumes are lost,
  public IPv4 addresses change unless they are Elastic IPs, and anything holding in-memory
  state dies. Only tag instances you are willing to lose the running state of.
- **Scope the tag deliberately.** The default `Schedule=office-hours` tag is what selects
  instances. A production instance tagged by accident will be stopped. Use the tag-scoped
  IAM policy so the credential physically cannot touch anything else.
- **Compute Optimizer savings are estimates.** They assume the workload keeps the same
  shape. Check the utilization metrics behind a recommendation before you resize something
  latency-sensitive.
- **Cost Explorer requests are billable** at roughly 0.01 USD each. Pass
  `--skip-recommendation` to drop one request per run.
- **The budget template creates real resources.** AWS Budgets is free for the first two
  budgets per account and about 0.02 USD per budget per day after that. Cost anomaly
  detection is free. SNS email delivery is effectively free at alert volumes.
- **CUR data is billing data.** A synced export contains every resource id and tag value in
  the account. `cur-data/` and `reports/` are gitignored here; keep them that way, and treat
  the generated HTML as confidential.
- **The current month is always partial.** CUR data for an in-progress month is incomplete
  and refreshes several times a day, so month-over-month comparisons against it will
  understate. The untagged drill labels a partial month in its report.
- **Untagged is not the same as fixable.** Most untagged spend is account-level — tax,
  support, registrar fees, data transfer — with no resource to attach a tag to. The drill
  separates the two so nobody is sent chasing the unfixable half.

## Validation

```bash
make check     # lint + tests + CloudFormation template validation
make test      # unit tests, every AWS call stubbed with botocore Stubber
make samples   # regenerate examples/output/ from the synthetic fixtures
```

The test suite makes no network calls and reads no real billing data. Live-API responses
come from [`tests/fixtures.py`](tests/fixtures.py), stubbed with botocore's `Stubber`, using
invented resource IDs and the reserved example account `111122223333`. CUR tests build
synthetic Parquet files in a temp directory from
[`tests/cur_fixtures.py`](tests/cur_fixtures.py). The committed sample output is generated
from the same live-API fixtures, and `make test` fails if it drifts out of sync.

## Related Daily DevOps Pages

- [AWS Cost Optimization Consultant: A Practical Strategy Guide](https://www.daily-devops.com/aws-cost-optimization-30-60-percent-reduction-strategies/)
- [AWS Cloud Automations That Cut Costs](https://www.daily-devops.com/aws-cloud-automations-that-cut-costs/)
- [Enterprise FinOps Automation: AWS Cost Governance at Scale](https://www.daily-devops.com/enterprise-finops-automation-aws-cost-governance/)
- [FinOps Implementation on AWS](https://www.daily-devops.com/finops-implementation-aws-cost-optimization-case-study/)
- [GitHub Organization Structure](https://www.daily-devops.com/github-organization-structure/)

## License

AGPL-3.0. See [LICENSE](LICENSE).
