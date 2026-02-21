#!/usr/bin/env python3
"""
extract_pdf.py — Airbus TSM Reset PDF → database.json extractor
Parses resets.pdf (TSM A318/A319/A320/A321 System Reset Guidelines, ATA 24-00-00-810-818-A)
using PyMuPDF (fitz) and outputs structured database.json.

Usage:
    pip install pymupdf
    python extract_pdf.py [--pdf resets.pdf] [--out database.json] [--verbose]

This script is a best-effort parser. The output should be reviewed and corrected
in the admin panel (admin.html) after generation.
"""

import re
import json
import sys
import os
import argparse
from datetime import date

try:
    import fitz  # PyMuPDF
except ImportError:
    print("ERROR: pymupdf is not installed. Run: pip install pymupdf")
    sys.exit(1)


# ── CONFIG ──────────────────────────────────────────────────────────────────
DEFAULT_PDF = "resets.pdf"
DEFAULT_OUT = "database.json"

# FSN range → aircraft type
def fsn_to_aircraft(fsn_marker: str) -> list[str]:
    """Convert an FSN marker string to aircraft list."""
    if not fsn_marker or "ALL" in fsn_marker.upper():
        return ["CEO", "NEO"]

    ceo_ranges = re.findall(r"0[5-9]\d-\d+", fsn_marker)  # 051-100 range
    neo_ranges  = re.findall(r"1\d\d-\d+", fsn_marker)    # 101-150 range

    has_ceo = bool(ceo_ranges) or re.search(r"05[1-9]|0[6-9]\d|100", fsn_marker)
    has_neo = bool(neo_ranges) or re.search(r"10[1-9]|1[1-4]\d|150", fsn_marker)

    aircraft = []
    if has_ceo:
        aircraft.append("CEO")
    if has_neo:
        aircraft.append("NEO")

    return aircraft if aircraft else ["CEO", "NEO"]


# ── TEXT EXTRACTION ──────────────────────────────────────────────────────────
def extract_pages(pdf_path: str, verbose: bool = False) -> list[dict]:
    """Extract text from each page with page number metadata."""
    doc = fitz.open(pdf_path)
    pages = []
    for i, page in enumerate(doc):
        text = page.get_text("text")
        if verbose:
            print(f"[Page {i+1}] {len(text)} chars")
        pages.append({"page": i + 1, "text": text})
    doc.close()
    return pages


# ── BLOCK SPLITTING ──────────────────────────────────────────────────────────
def split_into_blocks(pages: list[dict]) -> list[dict]:
    """
    Split the full document text into SUBTASK blocks.
    Each SUBTASK block corresponds to one reset procedure entry.
    """
    full_text = "\n".join(p["text"] for p in pages)
    # Add page boundary markers for debugging
    full_with_markers = ""
    for p in pages:
        full_with_markers += f"\n<<<PAGE {p['page']}>>>\n" + p["text"]

    # Split on SUBTASK headers
    subtask_pattern = re.compile(
        r"(SUBTASK\s+[\d-]+\s*[-–]\s*[\w\s]+)",
        re.IGNORECASE
    )

    blocks = []
    splits = list(subtask_pattern.finditer(full_with_markers))

    for idx, match in enumerate(splits):
        start = match.start()
        end = splits[idx + 1].start() if idx + 1 < len(splits) else len(full_with_markers)
        block_text = full_with_markers[start:end]

        # Find page number from preceding marker
        preceding = full_with_markers[:start]
        page_markers = re.findall(r"<<<PAGE (\d+)>>>", preceding)
        page_num = int(page_markers[-1]) if page_markers else 0

        blocks.append({
            "subtask_header": match.group(0).strip(),
            "text": block_text,
            "page": page_num
        })

    return blocks


# ── SINGLE BLOCK PARSER ──────────────────────────────────────────────────────
def parse_block(block: dict) -> dict | None:
    """Parse one SUBTASK block into a structured entry."""
    text = block["text"]
    page = block["page"]

    # ── FSN applicability ──
    fsn_match = re.search(
        r"\*\*\s*ON\s+A/C\s+FSN\s+([\d\s,\-–]+(?:TO|AND|OR|,|[\d\s])*)",
        text, re.IGNORECASE
    )
    fsn_str = fsn_match.group(1).strip() if fsn_match else "ALL"
    aircraft = fsn_to_aircraft(fsn_str)

    # ── ATA chapter ──
    ata_match = re.search(r"(?:ATA|TASK)\s+(\d{2})[-–\s]", text)
    ata = ata_match.group(1) if ata_match else "00"

    # ── ECAM message extraction ──
    # Look for alert names in all-caps (typical ECAM format)
    ecam_pattern = re.compile(
        r"(?:ECAM[^:\n]*[:–\-]\s*|ALERT[^:\n]*[:–\-]\s*|MESSAGE[^:\n]*[:–\-]\s*)"
        r"([A-Z][A-Z0-9 /\+]+)",
        re.IGNORECASE
    )
    ecam_msgs = list(dict.fromkeys(
        m.group(1).strip()
        for m in ecam_pattern.finditer(text)
        if len(m.group(1).strip()) > 4
    ))

    # Fallback: look for known ECAM patterns (FWC, PACK, LGCIU etc.)
    if not ecam_msgs:
        known = re.findall(
            r"\b([A-Z]{2,4}(?:\s+[A-Z]{2,})+(?:\s+\d+)?(?:\s+FAULT)?)\b",
            text
        )
        ecam_msgs = list(dict.fromkeys(k.strip() for k in known if len(k) > 6))[:4]

    if not ecam_msgs:
        # Use subtask header as fallback
        ecam_msgs = [block["subtask_header"].replace("SUBTASK", "").strip()]

    # ── Computer / affected system ──
    comp_match = re.search(
        r"(?:COMPUTER|AFFECTED COMPUTER|SYSTEM)[:\s]+([A-Z0-9/\-]+(?:\s+[A-Z0-9/\-]+)?)",
        text, re.IGNORECASE
    )
    computer = comp_match.group(1).strip() if comp_match else ""

    # ── Reset procedure ──
    proc_match = re.search(
        r"(?:RESET PROCEDURE|PROCEDURE)[:\s]*([\s\S]+?)(?=CIRCUIT BREAKER|WARNING|CAUTION|NOTE|SUBTASK|\Z)",
        text, re.IGNORECASE
    )
    raw_proc = proc_match.group(1).strip() if proc_match else ""

    # Clean and number steps
    steps = []
    for line in raw_proc.split("\n"):
        line = line.strip()
        if not line or len(line) < 5:
            continue
        # Strip existing numbers
        line = re.sub(r"^\d+[\.\)]\s*", "", line)
        steps.append(line)

    reset_procedure = "\n".join(f"{i+1}. {s}" for i, s in enumerate(steps)) if steps else raw_proc

    # ── Warnings ──
    warnings = []
    for w in re.finditer(r"WARNING\s*[:\-]?\s*(.+?)(?=CAUTION|NOTE|WARNING|\n\n|\Z)", text, re.IGNORECASE | re.DOTALL):
        txt = w.group(1).strip().replace("\n", " ")
        if txt and len(txt) > 10:
            warnings.append(txt)

    # ── Cautions ──
    cautions = []
    for c in re.finditer(r"CAUTION\s*[:\-]?\s*(.+?)(?=WARNING|NOTE|CAUTION|\n\n|\Z)", text, re.IGNORECASE | re.DOTALL):
        txt = c.group(1).strip().replace("\n", " ")
        if txt and len(txt) > 10:
            cautions.append(txt)

    # ── Notes ──
    note_match = re.search(
        r"NOTE\s*[:\-]?\s*([\s\S]+?)(?=WARNING|CAUTION|CIRCUIT|SUBTASK|\Z)",
        text, re.IGNORECASE
    )
    notes = note_match.group(1).strip().replace("\n", " ") if note_match else ""

    # ── Circuit breaker table ──
    cb_table = parse_cb_table(text, fsn_str)

    # ── Build unique ID ──
    id_base = ecam_msgs[0].lower() if ecam_msgs else block["subtask_header"].lower()
    id_str = re.sub(r"[^a-z0-9]+", "-", id_base).strip("-")[:60] + f"-p{page}"

    return {
        "id": id_str,
        "aircraft": aircraft,
        "ecamMessages": ecam_msgs,
        "ata": ata,
        "computer": computer,
        "resetProcedure": reset_procedure,
        "notes": notes,
        "warnings": warnings,
        "cautions": cautions,
        "cbTable": cb_table,
        "_source_page": page,
        "_fsn_raw": fsn_str
    }


def parse_cb_table(text: str, default_fsn: str = "ALL") -> list[dict]:
    """Extract circuit breaker table rows from block text."""
    cb_entries = []

    # Pattern: PANEL | DESIGNATION | FIN | LOCATION | (FSN)
    # Try to match tabular rows with at least panel + FIN
    cb_patterns = [
        # Full row: 49VU  AIR COND/ACSC1/SPLY  1CA1  D01  101-150
        re.compile(
            r"(\d{2,3}VU)\s+([\w/\- ]+?)\s+(\w{2,6})\s+([A-Z]\d{2})\s*([\d\-,\s]*)",
            re.IGNORECASE
        ),
        # Partial: FIN followed by location
        re.compile(
            r"\b(\d\w{1,5})\b\s+(?:AT\s+)?([A-Z]\d{2})\b",
            re.IGNORECASE
        )
    ]

    for pattern in cb_patterns:
        for match in pattern.finditer(text):
            groups = match.groups()
            if len(groups) >= 4:
                cb_entries.append({
                    "panel": groups[0].strip(),
                    "designation": groups[1].strip(),
                    "fin": groups[2].strip(),
                    "location": groups[3].strip(),
                    "fsn": groups[4].strip() if len(groups) > 4 and groups[4].strip() else default_fsn
                })
        if cb_entries:
            break

    # Deduplicate by FIN
    seen_fins = set()
    unique = []
    for cb in cb_entries:
        if cb["fin"] not in seen_fins:
            seen_fins.add(cb["fin"])
            unique.append(cb)

    return unique[:10]  # Cap at 10 rows


# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Extract ECAM resets from Airbus TSM PDF")
    parser.add_argument("--pdf", default=DEFAULT_PDF, help=f"Path to PDF (default: {DEFAULT_PDF})")
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"Output JSON file (default: {DEFAULT_OUT})")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--merge", action="store_true",
        help="Merge with existing database.json (append new entries, keep existing by ID)")
    args = parser.parse_args()

    if not os.path.exists(args.pdf):
        print(f"ERROR: PDF not found: {args.pdf}")
        sys.exit(1)

    print(f"Opening: {args.pdf}")
    pages = extract_pages(args.pdf, args.verbose)
    print(f"Extracted {len(pages)} pages")

    blocks = split_into_blocks(pages)
    print(f"Found {len(blocks)} SUBTASK blocks")

    messages = []
    skipped = 0
    for i, block in enumerate(blocks):
        try:
            entry = parse_block(block)
            if entry:
                messages.append(entry)
                if args.verbose:
                    print(f"  [{i+1}/{len(blocks)}] ✓ {entry['ecamMessages'][0][:50]} (ATA {entry['ata']}, p.{entry['_source_page']})")
            else:
                skipped += 1
        except Exception as e:
            skipped += 1
            if args.verbose:
                print(f"  [{i+1}/{len(blocks)}] ✗ SKIP: {e}")

    # Merge with existing if requested
    if args.merge and os.path.exists(args.out):
        with open(args.out, "r") as f:
            existing = json.load(f)
        existing_ids = {m["id"] for m in existing.get("messages", [])}
        new_entries = [m for m in messages if m["id"] not in existing_ids]
        messages = existing["messages"] + new_entries
        print(f"Merged: {len(new_entries)} new + {len(existing_ids)} existing = {len(messages)} total")

    output = {
        "version": "1.0",
        "lastUpdated": str(date.today()),
        "source": args.pdf,
        "messages": messages
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\n✓ Wrote {len(messages)} entries to {args.out} ({skipped} blocks skipped)")
    print(f"\nREVIEW RECOMMENDED:")
    print(f"  1. Open admin.html in a browser")
    print(f"  2. Check each entry for correct ECAM messages, procedures, and CB tables")
    print(f"  3. Export updated database.json and commit to repo")


if __name__ == "__main__":
    main()
