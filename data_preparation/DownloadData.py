"""
Download selected questions and their required documents (PDFs or Images).
Configured for the Finance domain.
"""

import sys
import os
from pathlib import Path

# Add project root to sys.path
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from data_preparation.unidoc_downloader import UniDocDownloader

# ==============================================================================
# ⚙️ CONFIGURATION: LIMITS & FORMAT SELECTION
# ==============================================================================
# 1. Target Domain: Set to 'finance'
DOMAIN = "finance"

# 2. Question Range / Limits
START_QID = 0          # Starting question index (e.g. 0)
END_QID = 2            # Ending question index (e.g. 5 means questions 0, 1, 2, 3, 4)

# 3. Format Choice:
# Set default format for all questions: "pdf" or "image"
DEFAULT_FORMAT = "pdf" # Set to "pdf" (to work with PDFs first) or "image"

# OR specify custom lists of question IDs to mix formats:
# Example: PDF_QUESTIONS = [0, 1], IMAGE_QUESTIONS = [2, 3, 4]
PDF_QUESTIONS = []     # If specified, these question IDs will use PDFs
IMAGE_QUESTIONS = []   # If specified, these question IDs will use Images

# Base directory resolution
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DATA = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "data"))
OUTPUT_DIR = PARENT_DATA if os.path.exists(PARENT_DATA) else os.path.join(SCRIPT_DIR, "data")
# ==============================================================================


def main():
    # Build per-question format dictionary if custom lists provided
    formats = {}
    for qid in PDF_QUESTIONS:
        formats[qid] = "pdf"
    for qid in IMAGE_QUESTIONS:
        formats[qid] = "image"

    downloader = UniDocDownloader()

    print("=" * 70)
    print("🎯 UniDoc-Bench Targeted Document Downloader (Finance Domain)")
    print("=" * 70)
    print(f"Domain         : {DOMAIN}")
    print(f"Question Range : QID {START_QID} to {END_QID - 1} (Total: {END_QID - START_QID})")
    print(f"Default Format : {DEFAULT_FORMAT.upper()}")
    if formats:
        print(f"Custom Formats : {formats}")
    print("-" * 70)

    # Downloads ONLY the documents needed to answer these questions
    result = downloader.download_questions(
        domain=DOMAIN,
        start_row=START_QID,
        end_row=END_QID,
        default_format=DEFAULT_FORMAT,
        formats=formats if formats else None,
        output_dir=OUTPUT_DIR,
        gt_only=False
    )

    questions = result["questions"]
    print(f"\n✓ Retrieved {len(questions)} questions across {len(result['unique_document_ids'])} unique documents.")
    print(f"  • PDF documents:   {result['total_pdfs']}")
    print(f"  • Image documents: {result['total_images']} page images")
    print(f"✓ Saved metadata:    {result.get('qa_file')}\n")

    print("=" * 70)
    print("📋 SELECTED QUESTIONS & FORMATS")
    print("=" * 70)
    for q in questions:
        fmt = q['format'].upper()
        print(f"\n[QID: {q['question_idx']} | Format: {fmt}]")
        print(f"• Question : {q['question']}")
        print(f"• Answer   : {q['answer']}")
        print(f"• Doc ID   : {q['document_id']}")
        if q['format'] == 'pdf':
            print(f"• Local PDF: {q['local_pdf']}")
        else:
            print(f"• Local Img: {len(q['local_images'])} pages ({q['local_images'][0]}...)")

    print("\n" + "=" * 70)
    print("✅ Ready! Next step: run `python upload_s3.py` to push to S3.")
    print("=" * 70)


if __name__ == "__main__":
    main()
