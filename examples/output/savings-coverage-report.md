$ python scripts/savings_coverage_report.py --days 30 --output markdown

| section        | metric                  | value    |
| -------------- | ----------------------- | -------- |
| utilization    | total_commitment        | 7,200.00 |
| utilization    | used_commitment         | 6,660.72 |
| utilization    | unused_commitment       | 539.28   |
| utilization    | utilization_pct         | 92.51    |
| utilization    | net_savings             | 2,184.36 |
| coverage       | covered_spend           | 6,660.72 |
| coverage       | on_demand_spend         | 3,120.44 |
| coverage       | coverage_pct            | 68.10    |
| recommendation | hourly_commitment       | 3.34     |
| recommendation | current_on_demand_spend | 3,120.44 |
| recommendation | est_monthly_savings     | 681.72   |
| recommendation | est_savings_pct         | 21.85    |
| recommendation | est_roi_pct             | 27.94    |
