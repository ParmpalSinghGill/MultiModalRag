"""
Docling Processor for Multimodal Document Parsing and Chunking.

Features:
- Configurable Tokenizers:
    1. 'text-embedding-3-small' / 'openai' (Default/Recommended: OpenAI tiktoken cl100k_base, 8,191 max window)
    2. 'amazon-titan-v2' (Amazon Titan Text Embeddings V2, 8,192 token window)
    3. 'gemini' (Google Gemini / Gemma 256k SentencePiece tokenizer, 2,048 max window)
    4. 'sentence-transformers' (all-MiniLM-L6-v2, 220 tokens safe limit)
- Deep Layout and Structural Parsing
- Dense Financial Table Extraction to Markdown
- High-Resolution Picture / Graph / Chart Cropping to Disk
- Automatic OCR Text Extraction from Images using Tesseract
- Graph vs Photo vs Diagram Classification and Metadata Extraction
- Semantic, Layout-Aware Multimodal Hybrid Chunks
"""

import sys
import os
import json
from pathlib import Path
from typing import Dict, Any, List, Optional

# Add project root to sys.path
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from data_ingestion.utils import ensure_cudnn_ld_path, debug_pause
from data_ingestion.rag_tokenizers import (
    create_tokenizer,
    OpenAITiktokenTokenizer,
    GeminiTokenizer,
    AmazonTitanTokenizer
)

# Resolve cuDNN Linux runtime dynamic link libraries for PyTorch + CUDA
ensure_cudnn_ld_path()

# Optional dependencies
try:
    import pytesseract
    PYTESSERACT_AVAILABLE = True
except ImportError:
    PYTESSERACT_AVAILABLE = False

from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.datamodel.base_models import InputFormat
from docling.chunking import HybridChunker
from docling_core.transforms.chunker.tokenizer.base import BaseTokenizer


# ==============================================================================
# 🚀 DOCLING MULTIMODAL PROCESSOR
# ==============================================================================

class DoclingProcessor:
    """
    Parses documents and extracts:
    - Clean Markdown prose
    - Dense tables with page provenance
    - Cropped image files of all graphs/charts with OCR text
    - Multimodal chunks sized according to selected tokenizer
    """

    def __init__(
        self,
        tokenizer_type: str = "text-embedding-3-small",
        max_tokens: int = 1000,
        extract_figures: bool = True,
        images_scale: float = 2.0,
        classify_figures: bool = True,
        deep_debug: bool = False
    ):
        self.tokenizer_type = tokenizer_type
        self.max_tokens = max_tokens
        self.extract_figures = extract_figures
        self.images_scale = images_scale
        self.classify_figures = classify_figures
        self.deep_debug = deep_debug

        print("[*] Initializing Docling DocumentConverter with high-res image pipeline & DocumentFigureClassifier-v2.5...")
        pipeline_options = PdfPipelineOptions()
        pipeline_options.generate_picture_images = extract_figures
        pipeline_options.images_scale = images_scale
        pipeline_options.do_ocr = True
        pipeline_options.do_picture_classification = classify_figures

        self.converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
            }
        )

        tokenizer_instance = create_tokenizer(tokenizer_type, max_tokens=max_tokens)
        self.chunker = HybridChunker(tokenizer=tokenizer_instance)

    def _debug_pause(self, stage_name: str, message: str = ""):
        """Helper to pause for user input when deep_debug is enabled."""
        if not self.deep_debug:
            return
        should_continue = debug_pause(stage_name, message, tag="DOCLING DEEP DEBUG")
        if not should_continue:
            self.deep_debug = False

    def process_document(
        self,
        file_path: str,
        doc_id: Optional[str] = None,
        figures_output_dir: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Parses a single document (PDF or image) using Docling,
        detects tables, extracts and OCRs graphs/pictures, and generates semantic chunks.
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        document_id = doc_id or os.path.basename(os.path.dirname(file_path)) or "unknown"
        filename = os.path.basename(file_path)

        print(f"[*] Parsing document '{document_id}' ({filename}) with Docling...")
        conv_result = self.converter.convert(file_path)
        doc = conv_result.document

        # 1. Export full document markdown
        markdown_text = doc.export_to_markdown()

        # 2. Extract structured tables with page numbers
        tables_data = []
        for idx, table in enumerate(doc.tables):
            try:
                tbl_md = table.export_to_markdown(doc=doc)
            except Exception:
                try:
                    tbl_md = table.export_to_markdown()
                except Exception:
                    tbl_md = ""
            page_no = table.prov[0].page_no if table.prov else None
            tables_data.append({
                "table_index": idx,
                "page_number": page_no,
                "markdown": tbl_md,
            })

            # # [DEEP DEBUG] Checkpoint 1: Table Extraction Inspection
            # if self.deep_debug:
            #     print("\n" + "=" * 76)
            #     print(f"🔍 [DEEP DEBUG] CHECKPOINT 1: TABLES EXTRACTED FOR '{document_id}'")
            #     print("=" * 76)
            #     print(f"Total Tables Detected: {len(tables_data)}")
            #     if tables_data:
            #         last_tbl = tables_data[-1]
            #         print(f"• Latest / Last Extracted Table (Index {last_tbl['table_index']} | Page {last_tbl['page_number']}):")
            #         print("-" * 76)
            #         tbl_lines = last_tbl['markdown'].strip().split("\n")
            #         preview = "\n".join(tbl_lines[:15])
            #         print(preview)
            #         if len(tbl_lines) > 15:
            #             print(f"... [{len(tbl_lines) - 15} more row(s)]")
            #         print("-" * 76)
            #     else:
            #         print("• No tables detected in this document.")
            #     self._debug_pause("Table Extraction", f"Inspected {len(tables_data)} table(s). Ready for pictures & figures.")
            

        # 3. Extract, Crop, and OCR Figures / Graphs / Images
        figures_data = []
        if self.extract_figures and hasattr(doc, "pictures"):
            doc_fig_dir = (
                os.path.join(figures_output_dir, document_id)
                if figures_output_dir
                else os.path.join(os.path.dirname(file_path), "figures")
            )
            os.makedirs(doc_fig_dir, exist_ok=True)

            for idx, pic in enumerate(doc.pictures):
                page_no = pic.prov[0].page_no if pic.prov else None
                caption = pic.caption_text(doc=doc) if hasattr(pic, "caption_text") else ""
                caption = caption.strip() if caption else ""

                bbox = None
                if pic.prov and hasattr(pic.prov[0], "bbox"):
                    b = pic.prov[0].bbox
                    bbox = {
                        "l": round(float(b.l), 2),
                        "t": round(float(b.t), 2),
                        "r": round(float(b.r), 2),
                        "b": round(float(b.b), 2),
                        "coord_origin": str(getattr(b, "coord_origin", "BOTTOMLEFT"))
                    }

                # Save cropped high-res image
                img = pic.get_image(doc)
                img_path = None
                img_size = None
                ocr_text = ""

                if img:
                    img_size = list(img.size)
                    fig_filename = f"{document_id}_fig_{idx:03d}.png"
                    fig_save_path = os.path.join(doc_fig_dir, fig_filename)
                    try:
                        img.save(fig_save_path)
                        img_path = os.path.abspath(fig_save_path)
                    except Exception as e:
                        print(f"    ⚠️ Warning saving figure {idx}: {e}")

                    # Run OCR to extract text/numbers inside chart
                    if PYTESSERACT_AVAILABLE:
                        try:
                            ocr_text = pytesseract.image_to_string(img).strip()
                        except Exception:
                            ocr_text = ""

                # 3.1 Extract neural classification from DocumentFigureClassifier-v2.5
                top_class = "unknown"
                top_confidence = 0.0
                all_predictions = []
                for annot in getattr(pic, "annotations", []):
                    if getattr(annot, "kind", "") == "classification":
                        preds = getattr(annot, "predicted_classes", [])
                        if preds:
                            top_class = preds[0].class_name
                            top_confidence = round(float(preds[0].confidence), 4)
                            all_predictions = [
                                {
                                    "class_name": c.class_name,
                                    "confidence": round(float(c.confidence), 4)
                                }
                                for c in preds[:5]
                            ]
                        break

                # 3.2 Map classifier predictions to semantic category
                CHART_CLASSES = {
                    "bar_chart", "line_chart", "pie_chart", "scatter_plot",
                    "box_plot", "flow_chart"
                }
                if top_class in CHART_CLASSES:
                    fig_type = "chart_graph"
                elif top_class == "table":
                    fig_type = "visual_table"
                elif top_class in ("logo", "icon", "qr_code", "bar_code", "signature", "stamp"):
                    fig_type = "icon_or_logo"
                elif top_class == "photograph":
                    fig_type = "photo"
                elif top_class in ("engineering_drawing", "geographical_map", "chemistry_structure"):
                    fig_type = "technical_diagram"
                elif img and (img.width < 60 or img.height < 60):
                    fig_type = "icon_or_logo"
                else:
                    fig_type = top_class if top_class != "unknown" else "figure_diagram"

                figures_data.append({
                    "figure_id": f"{document_id}_fig_{idx:03d}",
                    "figure_index": idx,
                    "page_number": page_no,
                    "type": fig_type,
                    "classifier_v2_5": {
                        "top_class": top_class,
                        "confidence": top_confidence,
                        "predictions": all_predictions,
                    },
                    "caption": caption,
                    "ocr_text": ocr_text,
                    "image_file": img_path,
                    "image_dimensions": img_size,
                    "bbox": bbox
                })

        # [DEEP DEBUG] Checkpoint 2: Picture & Neural Classifier Inspection
        if self.deep_debug:
            print("\n" + "=" * 76)
            print(f"🔍 [DEEP DEBUG] CHECKPOINT 2: PICTURES & CLASSIFICATIONS FOR '{document_id}'")
            print("=" * 76)
            print(f"Total Figures Extracted: {len(figures_data)}")
            for fig in figures_data[:6]:
                clf = fig.get("classifier_v2_5", {})
                top_c = clf.get("top_class", "unknown")
                conf = clf.get("confidence", 0.0)
                preds = clf.get("predictions", [])
                preds_str = ", ".join([f"{p['class_name']}: {p['confidence']*100:.1f}%" for p in preds[:3]])
                print(f"\n📸 Figure [{fig['figure_id']}] (Page {fig['page_number']}) -> Category: {fig['type']}")
                print(f"   • Neural Classifier Class : {top_c} ({conf*100:.1f}% confidence)")
                if preds_str:
                    print(f"   • Top Predictions         : {preds_str}")
                print(f"   • Caption                 : {fig['caption'] or '(No caption detected)'}")
                print(f"   • Image File              : {fig['image_file']} (Dims: {fig['image_dimensions']})")
                if fig['ocr_text']:
                    ocr_snippet = ' '.join(fig['ocr_text'].split()[:1000])
                    print(f"   • OCR Extracted Text      : {ocr_snippet}  ...")
            if len(figures_data) > 6:
                print(f"\n... and {len(figures_data) - 6} more figure(s) extracted.")
            self._debug_pause("Picture & Figure Extraction", f"Inspected {len(figures_data)} figure(s). Ready for chunking.")

        # 4. Generate Semantic Layout-Aware Text & Table Chunks
        raw_chunks = list(self.chunker.chunk(doc))
        print(f"    ✓ Converted {len(doc.pages)} pages: {len(doc.tables)} tables, {len(figures_data)} pictures/graphs, {len(raw_chunks)} prose chunks.")

        structured_chunks = []
        for i, chunk in enumerate(raw_chunks):
            meta_dict = {}
            doc_items = getattr(chunk.meta, "doc_items", []) if hasattr(chunk, "meta") else []
            if hasattr(chunk, "meta"):
                try:
                    if hasattr(chunk.meta, "model_dump"):
                        meta_dict = chunk.meta.model_dump()
                    elif hasattr(chunk.meta, "export_json_dict"):
                        meta_dict = chunk.meta.export_json_dict()
                except Exception:
                    pass

            headings = meta_dict.get("headings", [])
            page_nos = [
                p.page_no
                for item in doc_items
                for p in getattr(item, "prov", [])
                if hasattr(p, "page_no")
            ]
            page_numbers = sorted(list(set(page_nos)))

            has_table = (
                any("table" in str(getattr(item, "label", "")).lower() for item in doc_items)
                or ("|" in chunk.text and "\n" in chunk.text)
            )

            tok_count = self.chunker.tokenizer.count_tokens(chunk.text)

            structured_chunks.append({
                "chunk_id": f"{document_id}_chunk_{i:04d}",
                "chunk_index": i,
                "document_id": document_id,
                "chunk_type": "text_or_table",
                "text": chunk.text,
                "token_count": tok_count,
                "headings": headings,
                "page_numbers": page_numbers,
                "has_table": has_table,
                "has_image": False,
                "char_length": len(chunk.text),
                "word_count": len(chunk.text.split()),
                "source_file": os.path.abspath(file_path)
            })

        # 5. Add Multimodal Figure/Graph Chunks
        # Creates dedicated indexable chunks for charts and graphs containing caption + OCR data + neural classification
        for fig in figures_data:
            if fig["type"] == "chart_graph" or fig["caption"] or fig["ocr_text"]:
                fig_label = fig["caption"] if fig["caption"] else f"Figure on Page {fig['page_number']}"
                clf_info = fig.get("classifier_v2_5", {})
                top_c = clf_info.get("top_class", "unknown")
                conf = clf_info.get("confidence", 0.0)

                chunk_lines = [
                    f"## Visual Chart / Graph: {fig_label}",
                    f"- Source Document: {filename} (Page {fig['page_number']})",
                    f"- Neural Classification: {top_c.replace('_', ' ').title()} ({conf*100:.1f}% confidence) [Model: DocumentFigureClassifier-v2.5]",
                    f"- Visual Category: {fig['type'].replace('_', ' ').title()}",
                ]
                if fig["caption"]:
                    chunk_lines.append(f"- Caption / Title: {fig['caption']}")
                if fig["ocr_text"]:
                    chunk_lines.append(f"- Extracted Chart Text & Data Points:\n{fig['ocr_text']}")
                if fig["image_file"]:
                    chunk_lines.append(f"- Image File: {fig['image_file']}")

                fig_chunk_text = "\n".join(chunk_lines)
                fig_tok_count = self.chunker.tokenizer.count_tokens(fig_chunk_text)

                structured_chunks.append({
                    "chunk_id": f"{document_id}_figchunk_{fig['figure_index']:03d}",
                    "chunk_index": len(structured_chunks),
                    "document_id": document_id,
                    "chunk_type": "figure_graph",
                    "text": fig_chunk_text,
                    "token_count": fig_tok_count,
                    "headings": [fig["caption"]] if fig["caption"] else ["Figure"],
                    "page_numbers": [fig["page_number"]] if fig["page_number"] else [],
                    "has_table": False,
                    "has_image": True,
                    "image_file": fig["image_file"],
                    "caption": fig["caption"],
                    "ocr_text": fig["ocr_text"],
                    "figure_type": fig["type"],
                    "neural_class": top_c,
                    "classification_confidence": conf,
                    "char_length": len(fig_chunk_text),
                    "word_count": len(fig_chunk_text.split()),
                    "source_file": os.path.abspath(file_path)
                })

        # [DEEP DEBUG] Checkpoint 3: Raw vs Structured Chunks Inspection
        if self.deep_debug:
            text_chunks = [c for c in structured_chunks if not c.get("has_image")]
            fig_chunks = [c for c in structured_chunks if c.get("has_image")]
            print("\n" + "=" * 76)
            print(f"🔍 [DEEP DEBUG] CHECKPOINT 3: STRUCTURED CHUNKS FOR '{document_id}'")
            print("=" * 76)
            print(f"Docling Raw Chunks       : {len(raw_chunks)}")
            print(f"Total Structured Chunks  : {len(structured_chunks)} (Text/Tables: {len(text_chunks)}, Visual Graphs: {len(fig_chunks)})")
            if text_chunks:
                sample_t = text_chunks[0]
                print(f"\n📄 Sample Text/Table Chunk [{sample_t['chunk_id']}] ({sample_t['token_count']} tokens | Page {sample_t['page_numbers']}):")
                print(f"   Headings: {sample_t['headings']}")
                print(f"   Preview : {sample_t['text'][:250]}...")
            if fig_chunks:
                sample_f = fig_chunks[0]
                print(f"\n📊 Sample Embedded Figure Chunk [{sample_f['chunk_id']}] ({sample_f['token_count']} tokens):")
                print(f"   Title   : {sample_f['headings']}")
                print(f"   Class   : {sample_f.get('neural_class')} ({sample_f.get('classification_confidence', 0)*100:.1f}%)")
                print(f"   Content :\n{sample_f['text'][:300]}...")
            self._debug_pause("Chunk Generation", f"Completed document '{document_id}'. Ready to proceed.")

        return {
            "document_id": document_id,
            "filename": filename,
            "local_path": os.path.abspath(file_path),
            "page_count": len(doc.pages),
            "table_count": len(doc.tables),
            "picture_count": len(figures_data),
            "total_chunks": len(structured_chunks),
            "tokenizer": {
                "type": self.tokenizer_type,
                "max_tokens": self.max_tokens
            },
            "markdown_text": markdown_text,
            "tables": tables_data,
            "figures": figures_data,
            "chunks": structured_chunks
        }

    def process_all_documents(
        self,
        raw_docs_dir: str,
        output_chunks_dir: str,
        doc_ids: Optional[List[str]] = None,
        s3_uploader: Optional[Any] = None
    ) -> Dict[str, Any]:
        """
        Processes documents in raw_docs_dir, saving individual chunk files,
        cropped figure images, syncing deduplicated figures to S3, and saving a master chunks manifest.
        """
        os.makedirs(output_chunks_dir, exist_ok=True)
        figures_base_dir = os.path.join(output_chunks_dir, "figures")
        os.makedirs(figures_base_dir, exist_ok=True)

        manifest_file = os.path.join(raw_docs_dir, "ingestion_manifest.json")
        manifest_data = {}
        if os.path.exists(manifest_file):
            try:
                with open(manifest_file, "r", encoding="utf-8") as f:
                    manifest_data = json.load(f)
            except Exception:
                pass

        all_doc_records = {}
        all_chunks = []
        all_figures = []

        doc_folders = [
            d for d in os.listdir(raw_docs_dir)
            if os.path.isdir(os.path.join(raw_docs_dir, d)) and d not in ("metadata", "__pycache__")
        ]
        if doc_ids:
            doc_folders = [d for d in doc_folders if d in doc_ids]

        print(f"[*] Processing {len(doc_folders)} document(s) with Docling [{self.tokenizer_type.upper()} tokenizer]...")

        for doc_id in sorted(doc_folders):
            doc_path = os.path.join(raw_docs_dir, doc_id)
            pdf_files = [f for f in os.listdir(doc_path) if f.lower().endswith(".pdf")]

            if pdf_files:
                target_file = os.path.join(doc_path, pdf_files[0])
            else:
                img_files = sorted([f for f in os.listdir(doc_path) if f.lower().endswith((".png", ".jpg", ".jpeg"))])
                if not img_files:
                    continue
                target_file = os.path.join(doc_path, img_files[0])

            doc_record = self.process_document(
                target_file,
                doc_id=doc_id,
                figures_output_dir=figures_base_dir
            )

            # Deduplicated S3 Figure Upload & Chunk Enrichment
            if s3_uploader is not None:
                print(f"[*] Syncing & deduplicating figures to S3 for Document '{doc_id}'...")
                s3_sync_results = s3_uploader.sync_document_figures(
                    doc_id=doc_id,
                    figures_dir=figures_base_dir
                )
                doc_record["chunks"] = s3_uploader.enrich_chunks_with_s3_links(
                    doc_record["chunks"],
                    s3_sync_results
                )
                # Also enrich figures list
                for fig in doc_record.get("figures", []):
                    img_f = fig.get("image_file")
                    if img_f and Path(img_f).name in s3_sync_results:
                        res = s3_sync_results[Path(img_f).name]
                        fig["s3_uri"] = res.get("s3_uri")
                        fig["s3_url"] = res.get("s3_url")
                        fig["sha256"] = res.get("sha256")

            # Link QA metadata if present
            related_q = (
                manifest_data.get("documents", {}).get(doc_id, {}).get("related_questions", [])
            )
            doc_record["related_questions"] = related_q

            # Save individual document chunks JSON
            doc_chunk_file = os.path.join(output_chunks_dir, f"{doc_id}_chunks.json")
            with open(doc_chunk_file, "w", encoding="utf-8") as f:
                json.dump(doc_record, f, indent=2)

            all_doc_records[doc_id] = {
                "document_id": doc_id,
                "filename": doc_record["filename"],
                "page_count": doc_record["page_count"],
                "table_count": doc_record["table_count"],
                "picture_count": doc_record["picture_count"],
                "figures_count": len(doc_record["figures"]),
                "chunk_count": doc_record["total_chunks"],
                "chunk_file": os.path.abspath(doc_chunk_file)
            }

            all_chunks.extend(doc_record["chunks"])
            all_figures.extend(doc_record["figures"])

        # Save consolidated all_chunks.json
        master_manifest_path = os.path.join(output_chunks_dir, "all_chunks.json")
        master_summary = {
            "tokenizer": {
                "type": self.tokenizer_type,
                "max_tokens": self.max_tokens
            },
            "total_documents": len(all_doc_records),
            "total_chunks": len(all_chunks),
            "total_figures": len(all_figures),
            "documents": all_doc_records,
            "figures": all_figures,
            "chunks": all_chunks
        }
        with open(master_manifest_path, "w", encoding="utf-8") as f:
            json.dump(master_summary, f, indent=2)

        return master_summary
