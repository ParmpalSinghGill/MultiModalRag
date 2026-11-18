#!/usr/bin/env python3
"""
Targeted Question-Centric Downloader for Salesforce/UniDoc-Bench.

Select a specific number of questions (e.g. 1 question, 2 questions, 5 questions)
or specific question indices, and download ONLY the documents required to answer
those questions.

Usage:
    # Select 5 questions and download only their required documents
    python download_unidoc.py -n 5

    # Select 1 question (URL from Hugging Face viewer)
    python download_unidoc.py "https://huggingface.co/datasets/Salesforce/UniDoc-Bench/viewer/default/commerce_manufacturing?row=0" -n 1

    # Select specific question indices (e.g. 0, 1, 4)
    python download_unidoc.py --questions 0,1,4

    # Download only the ground-truth answer pages for 5 questions
    python download_unidoc.py -n 5 --gt-only

    # Browse/list available questions in the domain without downloading images
    python download_unidoc.py --list
"""

import sys
import os
import argparse
import json
from pathlib import Path
from typing import List, Optional

# Add project root to sys.path
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from data_preparation.unidoc_downloader.downloader import UniDocDownloader
from data_preparation.unidoc_downloader.parser import parse_hf_viewer_url


def format_bytes(num_bytes: int) -> str:
    """Formats bytes to human-readable string."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if num_bytes < 1024.0:
            return f"{num_bytes:.2f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.2f} TB"


def parse_question_indices(q_str: str) -> List[int]:
    """
    Parses strings like '0,1,2', '0-4', '0, 3-5'.
    """
    indices = set()
    for part in q_str.split(','):
        part = part.strip()
        if '-' in part:
            start, end = part.split('-', 1)
            indices.update(range(int(start), int(end) + 1))
        elif part.isdigit():
            indices.add(int(part))
    return sorted(list(indices))


def main():
    parser = argparse.ArgumentParser(
        description="Select N questions and download ONLY the documents required to answer them."
    )
    parser.add_argument(
        "url",
        nargs="?",
        default="https://huggingface.co/datasets/Salesforce/UniDoc-Bench/viewer/default/commerce_manufacturing?row=0",
        help="HuggingFace viewer URL or dataset identifier."
    )
    parser.add_argument(
        "-n", "--num-questions",
        type=int,
        default=None,
        help="Number of questions to select and download (e.g. 1, 2, 5). Default is 1 if not specified."
    )
    parser.add_argument(
        "-q", "--questions",
        type=str,
        default=None,
        help="Specific question indices or ranges (e.g. '0,1,2', '0-4'). Overrides -n."
    )
    parser.add_argument(
        "--start-row",
        type=int,
        default=None,
        help="Starting row index when selecting N questions. Defaults to URL row or 0."
    )
    parser.add_argument(
        "--domain",
        type=str,
        default=None,
        help="Domain / split name (e.g. commerce_manufacturing, construction, crm, etc.). Overrides URL if given."
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="./data",
        help="Target root directory to store downloaded files. Defaults to ./data."
    )
    parser.add_argument(
        "--gt-only",
        action="store_true",
        help="Download only the ground-truth answer page(s) for the questions, rather than the entire document."
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available questions in the domain and exit without downloading document images."
    )
    parser.add_argument(
        "--token",
        type=str,
        default=None,
        help="Hugging Face API token (optional for public datasets)."
    )
    parser.add_argument(
        "--upload-s3",
        action="store_true",
        help="Upload the required documents directly to AWS S3."
    )
    parser.add_argument(
        "--bucket",
        type=str,
        default="multimodal-rag-data-nov14",
        help="AWS S3 bucket name. Defaults to multimodal-rag-data-nov14."
    )
    parser.add_argument(
        "--s3-prefix",
        type=str,
        default="router",
        help="S3 folder prefix. Defaults to 'router'."
    )
    parser.add_argument(
        "--profile",
        type=str,
        default="rag-dev",
        help="AWS CLI profile name. Defaults to 'rag-dev'."
    )

    args = parser.parse_args()

    # Parse URL
    parsed = parse_hf_viewer_url(args.url)
    domain = args.domain or parsed["split"] or "commerce_manufacturing"
    start_row = args.start_row if args.start_row is not None else (parsed["row"] or 0)

    downloader = UniDocDownloader(repo_id=parsed.get("repo_id", "Salesforce/UniDoc-Bench"), token=args.token)

    # 1. Handle --list
    if args.list:
        print("=" * 72)
        print(f"📖 Available Questions in Domain: '{domain}'")
        print("=" * 72)
        items = downloader.list_questions(domain, count=15, start_row=start_row, output_dir=args.output_dir)
        for it in items:
            print(f"[{it['row_idx']}] {it['question']}")
            print(f"    • Doc ID: {it['document_id']} ({it['page_count']} pages) | Type: {it['question_type']}")
            print(f"    • Answer: {it['answer'][:90]}...")
            print()
        print("To download questions, run:")
        print(f"  python download_unidoc.py --domain {domain} -n 5")
        print(f"  python download_unidoc.py --domain {domain} --questions 0,1,2")
        return

    # Determine question selection
    indices = None
    count = args.num_questions

    if args.questions:
        indices = parse_question_indices(args.questions)
        count = len(indices)
    elif count is None:
        count = 1  # Default to 1 question (e.g. row 0 from viewer URL)

    print("=" * 72)
    print("🎯 UniDoc-Bench Question-Centric Minimal Downloader")
    print("=" * 72)
    print(f"Selected Domain     : {domain}")
    print(f"Questions Requested : {count} question(s)")
    if indices:
        print(f"Question Indices    : {indices}")
    else:
        print(f"Question Range      : Rows {start_row} to {start_row + count - 1}")
    print(f"Download Mode       : {'Ground-Truth Answer Pages Only' if args.gt_only else 'Full Required Documents'}")
    print(f"Output Directory    : {os.path.abspath(args.output_dir)}")
    print("-" * 72)

    # Fetch domain QA parquet metadata first (~57 KB)
    parquet_path = downloader.download_split_parquet(domain, args.output_dir)
    print(f"✓ Domain QA table loaded: {parquet_path} ({format_bytes(os.path.getsize(parquet_path))})\n")

    # Download questions and ONLY their required documents
    print("[*] Retrieving questions and identifying required documents...")
    result = downloader.download_questions(
        domain=domain,
        count=count,
        indices=indices,
        start_row=start_row,
        output_dir=args.output_dir,
        gt_only=args.gt_only
    )

    questions = result["questions"]

    # 1. Display Selected Questions
    print("\n" + "=" * 72)
    print(f"📋 SELECTED QUESTIONS ({len(questions)})")
    print("=" * 72)

    for i, q in enumerate(questions, 1):
        print(f"\n[Question #{i} | Index: {q['question_idx']}]")
        print(f"• Question       : {q['question']}")
        print(f"• Target Answer  : {q['answer']}")
        print(f"• Required Doc   : Document ID '{q['document_id']}'")
        print(f"• Question Type  : {q['question_type']} ({q['answer_type']})")
        print(f"• Document Pages : {len(q['longdoc_image_paths_rel'])} pages total")
        print(f"• Answer Found In: {', '.join(q['gt_image_paths_rel'])}")
        print(f"• Local Images   : {len(q['local_images'])} file(s) downloaded")

    # 2. Document & Download Summary
    total_bytes = 0
    downloaded_files = set()
    for q in questions:
        for p in q["local_images"]:
            downloaded_files.add(p)

    total_bytes = sum(os.path.getsize(f) for f in downloaded_files if os.path.exists(f))
    total_bytes += os.path.getsize(parquet_path)
    if "qa_file" in result and os.path.exists(result["qa_file"]):
        total_bytes += os.path.getsize(result["qa_file"])

    print("\n" + "=" * 72)
    print("📦 REQUIRED DOCUMENTS & DOWNLOAD SUMMARY")
    print("=" * 72)
    print(f"Questions Selected      : {len(questions)}")
    print(f"Unique Documents Needed : {len(result['unique_document_ids'])} ({', '.join(result['unique_document_ids'])})")
    pages_count = result.get('total_images', result.get('total_pages_downloaded', 0))
    print(f"Document Pages Download : {pages_count} image file(s)")
    print(f"Total Disk Usage        : {format_bytes(total_bytes)}")
    print(f"Saved Benchmark File    : {result.get('qa_file')}")
    print(f"Data Root Directory     : {os.path.abspath(args.output_dir)}/{domain}")
    print("=" * 72)

    # 3. Optional S3 Upload
    if args.upload_s3:
        from unidoc_downloader.s3_uploader import S3DocumentUploader
        print("\n" + "=" * 72)
        print("☁️ PUSHING REQUIRED DOCUMENTS TO AWS S3")
        print("=" * 72)
        uploader = S3DocumentUploader(bucket_name=args.bucket, profile_name=args.profile)
        upload_res = uploader.upload_questions_dataset(
            download_result=result,
            prefix=args.s3_prefix,
            upload_metadata=True
        )
        print("\n" + "=" * 72)
        print("✅ AWS S3 UPLOAD COMPLETE")
        print("=" * 72)
        print(f"Bucket           : {upload_res['bucket']}")
        print(f"Base Prefix      : {upload_res['prefix']}/")
        print(f"Uploaded Docs    : {', '.join(upload_res['uploaded_documents'])}")
        print(f"Total Pages in S3: {upload_res['total_pages_uploaded']}")
        if upload_res.get("metadata_s3_uri"):
            print(f"Metadata JSON    : {upload_res['metadata_s3_uri']}")
        print("=" * 72)


if __name__ == "__main__":
    main()
