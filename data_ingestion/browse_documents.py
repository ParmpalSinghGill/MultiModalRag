"""
Interactive Document & Chunk Inspector for OpenSearch.

Features:
- Inspect indexed documents, chunks, and metadata directly in terminal.
- Filter by Document ID, Chunk Type (tables, figures, text), or Page Number.
- Shows neural classification of figures and deterministic S3 URLs.
- Displays full OpenSearch index stats and document counts.
"""

import os
import sys
import json
import argparse
from pathlib import Path
from typing import Dict, Any, List, Optional

# Add project root to sys.path
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from opensearchpy import OpenSearch


class DocumentInspector:
    def __init__(self, host: str = "localhost", port: int = 9200, index_name: str = "multimodal_rag_finance"):
        self.client = OpenSearch(
            hosts=[{"host": host, "port": port}],
            http_compress=True,
            use_ssl=False,
            verify_certs=False,
            timeout=10
        )
        self.index_name = index_name

    def get_stats(self) -> Dict[str, Any]:
        """Returns high-level statistics about indexed documents and chunks."""
        count_res = self.client.count(index=self.index_name)
        total_chunks = count_res.get("count", 0)

        # Aggregation by document_id and chunk_type
        agg_body = {
            "size": 0,
            "aggs": {
                "by_doc": {
                    "terms": {"field": "document_id", "size": 50},
                    "aggs": {
                        "by_type": {"terms": {"field": "chunk_type"}}
                    }
                },
                "total_figures": {
                    "filter": {"term": {"has_image": True}}
                },
                "total_tables": {
                    "filter": {"term": {"has_table": True}}
                }
            }
        }
        agg_res = self.client.search(index=self.index_name, body=agg_body)
        doc_buckets = agg_res.get("aggregations", {}).get("by_doc", {}).get("buckets", [])
        fig_count = agg_res.get("aggregations", {}).get("total_figures", {}).get("doc_count", 0)
        tbl_count = agg_res.get("aggregations", {}).get("total_tables", {}).get("doc_count", 0)

        doc_breakdown = {}
        for b in doc_buckets:
            doc_id = b["key"]
            types = {tb["key"]: tb["doc_count"] for tb in b.get("by_type", {}).get("buckets", [])}
            doc_breakdown[doc_id] = {
                "total_chunks": b["doc_count"],
                "types": types
            }

        return {
            "index_name": self.index_name,
            "total_chunks": total_chunks,
            "total_visual_figures": fig_count,
            "total_tables": tbl_count,
            "documents": doc_breakdown
        }

    def browse(
        self,
        doc_id: Optional[str] = None,
        chunk_type: Optional[str] = None,
        has_image: Optional[bool] = None,
        has_table: Optional[bool] = None,
        limit: int = 10,
        page_no: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Queries chunks with specific filters."""
        filters = []
        if doc_id:
            filters.append({"term": {"document_id": doc_id}})
        if chunk_type:
            filters.append({"term": {"chunk_type": chunk_type}})
        if has_image is not None:
            filters.append({"term": {"has_image": has_image}})
        if has_table is not None:
            filters.append({"term": {"has_table": has_table}})
        if page_no is not None:
            filters.append({"term": {"page_numbers": page_no}})

        query = {"bool": {"filter": filters}} if filters else {"match_all": {}}
        body = {
            "size": limit,
            "query": query,
            "sort": [{"chunk_index": {"order": "asc"}}]
        }

        res = self.client.search(index=self.index_name, body=body)
        hits = res.get("hits", {}).get("hits", [])
        return [h["_source"] for h in hits]


def main():
    parser = argparse.ArgumentParser(description="Inspect chunks and documents in OpenSearch.")
    parser.add_argument("--stats", action="store_true", help="Show index overview and breakdown by document")
    parser.add_argument("--doc-id", type=str, default=None, help="Filter by document ID (e.g. 3152577)")
    parser.add_argument("--figures", action="store_true", help="List visual figure/chart chunks with S3 URLs")
    parser.add_argument("--tables", action="store_true", help="List financial table chunks with Markdown")
    parser.add_argument("--page", type=int, default=None, help="Filter by page number")
    parser.add_argument("--limit", type=int, default=5, help="Number of chunks to show")

    args = parser.parse_args()
    inspector = DocumentInspector()

    if args.stats or (not args.doc_id and not args.figures and not args.tables):
        stats = inspector.get_stats()
        print("=" * 76)
        print("📊 OPENSEARCH INDEX STATISTICS")
        print("=" * 76)
        print(f"Index Name       : {stats['index_name']}")
        print(f"Total Chunks     : {stats['total_chunks']}")
        print(f"Visual Figures   : {stats['total_visual_figures']}")
        print(f"Structured Tables: {stats['total_tables']}")
        print("\nDocument Breakdown:")
        print("-" * 76)
        for d_id, d_info in stats["documents"].items():
            type_str = ", ".join(f"{k}: {v}" for k, v in d_info["types"].items())
            print(f"  • Doc {d_id:<10}: {d_info['total_chunks']:>3} chunks [{type_str}]")
        print("=" * 76)

        if not args.figures and not args.tables and not args.doc_id:
            print("\nTip: Run with --figures, --tables, or --doc-id <ID> to inspect specific chunks.")
            return

    # Filtered Query
    has_img = True if args.figures else None
    has_tbl = True if args.tables else None

    chunks = inspector.browse(
        doc_id=args.doc_id,
        has_image=has_img,
        has_table=has_tbl,
        page_no=args.page,
        limit=args.limit
    )

    print(f"\nFound {len(chunks)} matching chunks:")
    print("-" * 76)

    for i, c in enumerate(chunks, 1):
        cid = c.get("chunk_id")
        doc = c.get("document_id")
        pages = c.get("page_numbers") or []
        page_s = f"Page {pages[0]}" if pages else "N/A"
        ctype = c.get("chunk_type", "").upper()

        print(f"#{i} [{cid}] Doc: {doc} ({page_s}) | Type: {ctype}")

        if c.get("neural_class"):
            conf = c.get("classification_confidence", 0.0)
            print(f"   🖼️  Neural Class: {c['neural_class']} ({conf*100:.1f}% confidence)")

        if c.get("image_s3_url"):
            print(f"   🌐 S3 Image URL : {c['image_s3_url']}")
            print(f"   📁 S3 URI       : {c.get('image_s3_uri')}")
            print(f"   💾 Local File   : {c.get('image_file')}")

        lines = [line.strip() for line in c.get("text", "").split("\n") if line.strip()]
        preview = "\n   ".join(lines[:6])
        print(f"   📝 Content Snippet:")
        print(f"   {preview}")
        if len(lines) > 6:
            print(f"   ... [{len(lines) - 6} more lines]")
        print()


if __name__ == "__main__":
    main()
