"""
Test script for Enhanced Auto-Categorization Service.

For each test item, displays:
  1. Taxonomy match and its linked client category
  2. All retrieval results (item-based + category-based) with similarity scores
  3. Final LLM output (category, confidence, reasoning)
"""

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Optional

from app.services.enhanced_auto_categorization_service import EnhancedAutoCategorizationService

logging.basicConfig(level=logging.WARNING)

DEFAULT_ITEMS = [
    "Laptop Dell Latitude 5540",
    "Office Chair Ergonomic Mesh",
    "Surgical Gloves Nitrile Powder-Free",
    "CCTV Camera 4MP Dome",
    "Diesel Generator 500 KVA",
    "LED Street Light 150W",
    "Fire Extinguisher ABC 6kg",
    "Printer Cartridge HP 26A",
    "Air Conditioning Unit Split 2 Ton",
    "Safety Helmet Hard Hat",
]


def load_items(args: argparse.Namespace) -> list[str]:
    """Resolve test items from CLI args, file, or defaults."""
    if args.file:
        path = Path(args.file)
        lines = path.read_text(encoding="utf-8").splitlines()
        items = [l.strip() for l in lines if l.strip() and not l.startswith("#")]
        print(f"Loaded {len(items)} items from {path}")
        return items
    if args.items:
        return args.items
    return DEFAULT_ITEMS


def fmt_table(headers: list[str], rows: list[list[str]], indent: int = 4) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    pad = " " * indent
    sep = pad + "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    hdr = pad + "|" + "|".join(f" {h:<{widths[i]}} " for i, h in enumerate(headers)) + "|"
    lines = [sep, hdr, sep]
    for row in rows:
        lines.append(pad + "|" + "|".join(f" {str(c):<{widths[i]}} " for i, c in enumerate(row)) + "|")
    lines.append(sep)
    return "\n".join(lines)


def banner(title: str):
    print(f"\n{'=' * 100}")
    print(f"  {title}")
    print(f"{'=' * 100}")


async def test_item(service: EnhancedAutoCategorizationService, description: str):
    banner(f'INPUT: "{description}"')

    # ── 1. Taxonomy retrieval (item-based hierarchical search) ──────────
    hier = service._search_hierarchical_levels(description, similarity_threshold=0.75, top_k=5)

    print("\n  [1] TAXONOMY RETRIEVAL (Item-Based Cosine Similarity)")
    if hier["success"]:
        best = hier["best_match"]
        print(f"      Best match level : {hier['matched_level']}")
        print(f"      Client category  : {best.get('client_category_name', 'N/A')}")
        print(f"      Category path    : {best.get('category_path', 'N/A')}")
        print(f"      Similarity       : {hier['similarity_score']:.4f}")

        matches = hier.get("all_level_matches", [])
        if matches:
            headers = ["#", "Item Description", "Client Category", "Category Path", "Similarity", "Level"]
            rows = []
            for i, m in enumerate(matches, 1):
                meta = m["metadata"]
                rows.append([
                    str(i),
                    (meta.get("item_description") or "")[:50],
                    meta.get("client_category_name", "N/A"),
                    meta.get("category_path", "N/A"),
                    f"{m['similarity_score']:.4f}",
                    m["matched_level"],
                ])
            print()
            print(fmt_table(headers, rows))
    else:
        print(f"      No taxonomy match found: {hier.get('reason', 'N/A')}")

    # ── 2. Category-name retrieval (hybrid search) ──────────────────────
    cat_results = service._search_by_category_name(description, top_k=5)

    print("\n  [2] CATEGORY-NAME RETRIEVAL (Hybrid Search)")
    if cat_results.get("success") and cat_results.get("matches"):
        headers = ["#", "Category Name", "Similarity", "Item Count"]
        rows = []
        for i, m in enumerate(cat_results["matches"], 1):
            rows.append([
                str(i),
                m["category_name"],
                f"{m['similarity']:.4f}",
                str(m.get("item_count", "N/A")),
            ])
        print(fmt_table(headers, rows))

        # Show hybrid re-ranking decision if taxonomy match exists
        if hier["success"]:
            hybrid = service._hybrid_category_selection(
                item_based_category=hier["best_match"]["client_category_name"],
                item_based_similarity=hier["similarity_score"],
                category_based_results=cat_results,
            )
            print(f"\n      Hybrid decision  : {hybrid['method']}")
            print(f"      Agreement        : {hybrid['agreement']}")
            print(f"      Final category   : {hybrid['final_category']}")
            print(f"      Confidence boost : +{hybrid.get('confidence_boost', 0):.2f}")
            print(f"      Reasoning        : {hybrid['reasoning']}")
    else:
        print(f"      No category-name matches: {cat_results.get('reason', 'N/A')}")

    # ── 3. Full categorize_item call (includes LLM decision) ────────────
    start = time.time()
    result = await service.categorize_item(
        item_description=description,
        user_id="test_script",
        rfq_id="TEST-001",
    )
    elapsed_ms = int((time.time() - start) * 1000)

    print(f"\n  [3] FINAL LLM OUTPUT")
    print(f"      Category         : {result.get('client_category', 'N/A')}")
    print(f"      Confidence       : {result.get('confidence_score', 0):.4f}")
    sim = result.get("similarity_score")
    print(f"      Similarity       : {f'{sim:.4f}' if isinstance(sim, (int, float)) else sim}")
    print(f"      Method           : {result.get('method', 'N/A')}")
    print(f"      Time             : {elapsed_ms} ms")

    reasoning = result.get("openai_reasoning") or result.get("reasoning", "")
    if reasoning:
        print(f"      Reasoning        : {reasoning}")

    if not result.get("success"):
        print(f"      Error            : {result.get('error', 'N/A')}")

    return result


async def main(items: list[str]):
    print("Initializing EnhancedAutoCategorizationService...")
    service = EnhancedAutoCategorizationService()
    print(f"  learning_taxonomy : {service.collection.count()} items")
    print(f"  category_names    : {service.category_collection.count()} categories")

    summary_rows = []
    for desc in items:
        r = await test_item(service, desc)
        sim = r.get("similarity_score")
        summary_rows.append([
            desc[:40],
            r.get("client_category", "N/A"),
            f"{r.get('confidence_score', 0):.2f}",
            f"{sim:.4f}" if isinstance(sim, (int, float)) else str(sim),
            r.get("method", "N/A"),
        ])

    banner("SUMMARY")
    print(fmt_table(
        ["Item", "Category", "Conf", "Sim", "Method"],
        summary_rows,
        indent=2,
    ))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Test the Enhanced Auto-Categorization Service",
        epilog="Examples:\n"
               "  python test_categorization.py\n"
               "  python test_categorization.py \"Diesel Generator\" \"Safety Helmet\"\n"
               '  python test_categorization.py -f items.txt\n',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("items", nargs="*", help="Item descriptions to categorize")
    parser.add_argument("-f", "--file", help="Text file with one item description per line")
    args = parser.parse_args()
    asyncio.run(main(load_items(args)))
