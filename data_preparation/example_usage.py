"""
Example script demonstrating how to use UniDocDownloader in Python
to select N questions (e.g. 5 questions) and download ONLY the documents
required to answer those questions.
"""

import sys
import os
from pathlib import Path
from PIL import Image

# Add project root to sys.path
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from data_preparation.unidoc_downloader import UniDocDownloader


def main():
    # 1. Initialize precision downloader
    downloader = UniDocDownloader()

    # 2. Select 5 questions from commerce_manufacturing
    # Downloads ONLY the documents needed to answer these 5 questions
    print("Selecting 5 questions and downloading only their required documents...\n")
    result = downloader.download_questions(
        domain="commerce_manufacturing",
        count=5,          # Select 5 questions (can be 1, 2, 5, etc.)
        output_dir="./data",
        gt_only=False     # Set True to download only ground-truth evidence pages
    )

    print("=" * 68)
    print(f"Retrieved {result['num_questions']} Questions across {len(result['unique_document_ids'])} Documents")
    print(f"Total Pages Downloaded: {result['total_pages_downloaded']}")
    print(f"Benchmark File: {result.get('qa_file')}")
    print("=" * 68)

    # 3. Iterate through each question and inspect its required document
    for i, q in enumerate(result["questions"], 1):
        print(f"\n[Question #{i} | Index {q['question_idx']}]")
        print(f"  • Question    : {q['question']}")
        print(f"  • Answer      : {q['answer']}")
        print(f"  • Required Doc: ID '{q['document_id']}' ({len(q['local_images'])} pages)")

        # First page of the document
        if q["local_images"]:
            first_page = q["local_images"][0]
            with Image.open(first_page) as img:
                print(f"  • Page 1 Image: {first_page} ({img.size})")

        # Ground truth page where the answer is found
        if q.get("gt_local_images"):
            print(f"  • Evidence Page: {q['gt_local_images'][0]}")


if __name__ == "__main__":
    main()
