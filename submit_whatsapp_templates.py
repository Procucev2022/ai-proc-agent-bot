"""
WhatsApp Template Submission Script

Submits RFQ and BFS Bid notification templates to ICS WhatsApp API for Meta approval.

Usage:
    python scripts/submit_whatsapp_templates.py [--dry-run]

Options:
    --dry-run    Print the payloads without submitting to API
"""

import sys
import json
import argparse
import requests
from pathlib import Path
from typing import Dict, Any, Optional

# Add project root to path for imports
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings


# Template definitions
TEMPLATES = {
    "rfq_notification": {
        "name": "qua_seller_rfq_notification",
        "language": "en",
        "category": "UTILITY",
        "structure": {
            "header": {
                "format": "TEXT",
                "text": "New RFQ Opportunity"
            },
            "body": (
                "A new Request For Quotation (RFQ) is available on QUA AI that matches your business profile.\n\n"
                "*RFQ ID:* {{1}}\n"
                "*Delivery Date:* {{2}}\n"
                "*Delivery Location:* {{3}}\n"
                "*Description:* {{4}}\n"
                "*Quantity:* {{5}}\n\n"
                "Click on \"I'm interested\" if you would like to submit a quote."
            ),
            "body_example": [
                "RFQ-2024-001",
                "15-Feb-2024",
                "Mumbai, Maharashtra",
                "Requirement for structural steel beams for commercial building project",
                "50 Tons"
            ],
            "footer": "Select an option to proceed",
            "buttons": [
                {
                    "text": "I'm Interested",
                    "type": "QUICK_REPLY"
                }
            ]
        }
    },
    "bfs_bid_notification": {
        "name": "qua_seller_bfs_bid_notification",
        "language": "en",
        "category": "UTILITY",
        "structure": {
            "header": {
                "format": "TEXT",
                "text": "New Bid Received"
            },
            "body": (
                "A buyer has placed a bid on your listed item on QUA AI.\n\n"
                "*Item:* {{1}}\n"
                "*Your Price:* {{2}}\n"
                "*Buyer Offer:* {{3}}\n"
                "*Buyer's Required Quantity:* {{4}}\n\n"
                "Please review and respond to this bid by clicking on the buttons below."
            ),
            "body_example": [
                "TMT Steel Bars 12mm",
                "55,000",
                "52,000",
                "100 tons"
            ],
            "footer": "Tap to respond",
            "buttons": [
                {
                    "text": "Accept Bid",
                    "type": "QUICK_REPLY"
                },
                {
                    "text": "Reject Bid",
                    "type": "QUICK_REPLY"
                }
            ]
        }
    }
}


def build_payload(template_config: Dict[str, Any], username: str, password: str) -> Dict[str, Any]:
    """Build the API payload for template creation."""
    return {
        "user": username,
        "pass": password,
        "createTemplate": template_config
    }


def submit_template(
    template_name: str,
    template_config: Dict[str, Any],
    api_url: str,
    username: str,
    password: str,
    dry_run: bool = False
) -> Optional[Dict[str, Any]]:
    """
    Submit a single template to the ICS API.

    Args:
        template_name: Human-readable name for logging
        template_config: Template configuration dict
        api_url: ICS template creation API URL
        username: WhatsApp API username
        password: WhatsApp API password
        dry_run: If True, print payload without submitting

    Returns:
        API response dict on success, None on failure
    """
    payload = build_payload(template_config, username, password)

    print(f"\n{'='*60}")
    print(f"Template: {template_name}")
    print(f"API Name: {template_config['name']}")
    print(f"{'='*60}")

    if dry_run:
        # Mask credentials in output
        display_payload = payload.copy()
        display_payload["user"] = "***MASKED***"
        display_payload["pass"] = "***MASKED***"
        print("\n[DRY RUN] Would submit payload:")
        print(json.dumps(display_payload, indent=2, ensure_ascii=False))
        return {"dry_run": True, "template_name": template_config["name"]}

    try:
        print(f"Submitting to: {api_url}")

        response = requests.post(
            api_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30
        )

        result = response.json()

        if response.status_code == 200 and result.get("code") == 200:
            print(f"✓ SUCCESS: Template submitted for approval")
            print(f"  Template ID: {result.get('data', {}).get('templateid')}")
            print(f"  Namespace: {result.get('data', {}).get('namespace')}")
            return result
        else:
            print(f"✗ FAILED: {result.get('message', 'Unknown error')}")
            print(f"  Response: {json.dumps(result, indent=2)}")
            return None

    except requests.exceptions.Timeout:
        print(f"✗ ERROR: Request timed out")
        return None
    except requests.exceptions.RequestException as e:
        print(f"✗ ERROR: Request failed - {e}")
        return None
    except json.JSONDecodeError:
        print(f"✗ ERROR: Invalid JSON response")
        print(f"  Raw response: {response.text[:500]}")
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Submit WhatsApp notification templates for Meta approval"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print payloads without submitting to API"
    )
    parser.add_argument(
        "--template",
        choices=["rfq", "bfs", "all"],
        default="all",
        help="Which template(s) to submit (default: all)"
    )
    args = parser.parse_args()

    # Load configuration
    try:
        settings = get_settings()
    except ValueError as e:
        print(f"Configuration error: {e}")
        sys.exit(1)

    # Get credentials
    username = settings.WHATSAPP_USERNAME
    password = settings.WHATSAPP_PASSWORD
    api_url = f"{settings.WHATSAPP_TEMPLATE_BASE_URL}/WhatsappTemplates/createTemplate"

    if not username or not password:
        print("Error: WHATSAPP_USERNAME and WHATSAPP_PASSWORD must be set in environment")
        sys.exit(1)

    print("WhatsApp Template Submission Tool")
    print("=" * 60)
    print(f"API URL: {api_url}")
    print(f"Username: {username[:3]}***{username[-3:] if len(username) > 6 else ''}")
    print(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE SUBMISSION'}")

    # Determine which templates to submit
    templates_to_submit = {}
    if args.template in ["rfq", "all"]:
        templates_to_submit["RFQ Notification"] = TEMPLATES["rfq_notification"]
    if args.template in ["bfs", "all"]:
        templates_to_submit["BFS Bid Notification"] = TEMPLATES["bfs_bid_notification"]

    # Submit templates
    results = {}
    for name, config in templates_to_submit.items():
        result = submit_template(
            template_name=name,
            template_config=config,
            api_url=api_url,
            username=username,
            password=password,
            dry_run=args.dry_run
        )
        results[name] = result

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    success_count = sum(1 for r in results.values() if r is not None)
    total_count = len(results)

    for name, result in results.items():
        status = "✓" if result else "✗"
        template_id = ""
        if result and not result.get("dry_run"):
            template_id = f" (ID: {result.get('data', {}).get('templateid', 'N/A')})"
        print(f"  {status} {name}{template_id}")

    print(f"\nTotal: {success_count}/{total_count} templates submitted successfully")

    if not args.dry_run:
        print("\nNext Steps:")
        print("  1. Templates are now pending Meta approval (typically 24-48 hours)")
        print("  2. Check approval status at: https://ngui.sendmsg.in/")
        print("  3. Once approved, update seller_notification_service.py to use template messages")

    return 0 if success_count == total_count else 1


if __name__ == "__main__":
    sys.exit(main())
