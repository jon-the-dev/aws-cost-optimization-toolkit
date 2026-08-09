$ python scripts/instance_scheduler.py --schedule "mon-fri 08:00-18:00" \
      --timezone America/New_York --output markdown
# evaluated at Saturday 2026-08-08 12:00 America/New_York

| instance_id         | name         | state   | desired | action |
| ------------------- | ------------ | ------- | ------- | ------ |
| i-055ee66ff77aa88bb | dev-worker-1 | stopped | stopped | none   |
| i-099cc00dd11ee22ff |              | running | stopped | stop   |
| i-0aa11bb22cc33dd44 | dev-api-1    | running | stopped | stop   |

Dry run: 2 instances would change state. Re-run with --apply to enforce.
