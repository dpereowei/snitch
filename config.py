import os
import sys

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")

# CRITICAL: Slack is not optional - this is a monitoring system
if not SLACK_WEBHOOK_URL:
    print("\n" + "="*80)
    print("CRITICAL ERROR: SLACK_WEBHOOK_URL environment variable is not set")
    print("="*80)
    print("\nYour monitoring system cannot function without Slack alerting.")
    print("Every anomaly must be delivered. Partial operation = blind system.")
    print("\nSet SLACK_WEBHOOK_URL before running:\n")
    print("  export SLACK_WEBHOOK_URL='https://hooks.slack.com/services/YOUR/WEBHOOK/URL'")
    print("\n" + "="*80 + "\n")
    sys.exit(1)

DB_URL = os.getenv("DB_URL", "sqlite:///events.db")
QUEUE_MAX_SIZE = int(os.getenv("QUEUE_MAX_SIZE", 5000))
WORKER_THREADS = int(os.getenv("WORKER_THREADS", 4))