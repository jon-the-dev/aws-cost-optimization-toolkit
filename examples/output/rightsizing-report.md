$ python scripts/rightsizing_report.py --region us-east-1 --output markdown

| resource_type | resource_id           | finding          | current    | recommended | monthly_savings | savings_pct |
| ------------- | --------------------- | ---------------- | ---------- | ----------- | --------------- | ----------- |
| ebs           | vol-0123456789abcdef0 | NotOptimized     | io1 500GiB | gp3 500GiB  | 597.50          | 78.00       |
| ec2           | reporting-worker-1    | Overprovisioned  | m5.4xlarge | m6i.xlarge  | 421.86          | 61.50       |
| lambda        | image-thumbnailer     | NotOptimized     | 3008MB     | 1024MB      | 64.18           | 52.70       |
| ec2           | batch-transcode       | Underprovisioned | r5.2xlarge | r6i.4xlarge | 0.00            | 0.00        |

Estimated monthly savings across 4 recommendations: 1,083.54 USD
