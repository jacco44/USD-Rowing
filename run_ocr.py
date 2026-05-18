#!/usr/bin/env python3
"""Run OCR on all pending WhatsApp scans. Safe to call from cron."""
import sys
from pathlib import Path

# Project root on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ocr_processor

if __name__ == "__main__":
    result = ocr_processor.process_all_pending()
    print(f"processed={result['processed']} errors={result['errors']}")
    sys.exit(1 if result["errors"] else 0)