#!/usr/bin/env python3
"""Run AWS Textract OCR on WhatsApp scan(s). Safe to call from cron or the wa_scraper."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

import ocr_processor  # noqa: E402


def main() -> int:
    if len(sys.argv) > 1:
        errors = 0
        for raw_id in sys.argv[1:]:
            try:
                scan_id = int(raw_id)
            except ValueError:
                print(f"Invalid scan id: {raw_id}", file=sys.stderr)
                errors += 1
                continue
            result = ocr_processor.process_scan(scan_id)
            if result.get("error"):
                print(f"scan #{scan_id}: {result['error']}", file=sys.stderr)
                errors += 1
            else:
                split = result.get("split_seconds")
                user = result.get("matched_username") or "no match"
                print(f"scan #{scan_id}: split={split}, user={user}")
        return 1 if errors else 0

    result = ocr_processor.process_all_pending()
    print(f"processed={result['processed']} errors={result['errors']}")
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
