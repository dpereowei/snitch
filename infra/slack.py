import logging
import requests
from config import SLACK_WEBHOOK_URL

logger = logging.getLogger("snitch.slack")

class SlackClient:
    def __init__(self):
        self.webhook = SLACK_WEBHOOK_URL
        logger.info(f"SlackClient initialized (webhook: {self.webhook[:30]}...)")

    def send(self, event):
        payload = self.format(event)

        try:
            r = requests.post(self.webhook, json=payload, timeout=3)
            if r.status_code != 200:
                raise Exception(f"Slack error: {r.text}")
            logger.debug(f"Slack notification sent: {event['id']}")
        except Exception as e:
            logger.error(f"Failed to send Slack notification: {e}")
            raise

    def format(self, e):
        event_type = e['type'].upper()
        emoji = "✅" if e['type'] == "test" else "🚨"
        
        return {
            "text": f"{emoji} {event_type}",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text":
                            f"*Pod:* {e['namespace']}/{e['pod_name']}\n"
                            f"*Node:* {e['node']}\n"
                            f"*Container:* {e.get('container_name')}\n"
                            f"*Reason:* {e.get('reason')}\n"
                            f"*Restart:* {e.get('restart_count')}\n"
                            f"*Message:* {e.get('message')}"
                    }
                }
            ]
        }