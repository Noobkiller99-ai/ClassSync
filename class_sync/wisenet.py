"""
wisenet.py — Course Outline PDF parser for SPJIMR.
"""
from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

@dataclass
class MandatorySessionInfo:
    course_code: str
    course_shortname: str
    mandatory_sessions: list[int]  # list of session numbers (ints)
    all_sessions: list[int] = field(default_factory=list)
    course_name: str = ""


def parse_mandatory_sessions_from_pdf(pdf_bytes: bytes, course_shortname: str) -> MandatorySessionInfo:
    """
    Parse a Course Outline PDF and extract session numbers marked as mandatory.

    The session plan table (spanning multiple pages) has columns:
        Session No & Faculty | ... | Mandatory Sessions

    The "Mandatory Sessions" column contains:
        - "Yes"              → all sessions in that row are mandatory
        - "Session N - Yes"  → only session N is mandatory
        - ""                 → not mandatory

    Because the table spans pages, we use two strategies:
    1. On any page that has the "Mandatory Sessions" header, we identify the
       exact column indices.
    2. On continuation pages (no header visible), we assume the table has the
       same structure: first column = session numbers, last column = mandatory flag.
    """
    try:
        import pdfplumber
    except ImportError:
        raise RuntimeError("pdfplumber is not installed. Run: pip install pdfplumber")

    course_code = course_shortname.split("-")[0].strip().upper()
    mandatory: list[int] = []
    all_sessions_found: list[int] = []
    extracted_course_name: str = ""

    # Global column config discovered from header row
    mandatory_col_idx: int | None = None
    session_col_idx: int | None = None
    in_session_table = False  # True once we've found the session plan table

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        # Extract course name from page 1 text
        if pdf.pages:
            p1_text = pdf.pages[0].extract_text() or ""
            m_name = re.search(r'Course\s+(?:Name|Title)\s*[:|-]?\s*([^\n\r]+)', p1_text, re.I)
            if m_name:
                raw_name = m_name.group(1).strip()
                cleaned_name = re.sub(r'\s*(?:Credits|Course Code|Term|Instructor|No\. of|Session).*$', '', raw_name, flags=re.I).strip()
                if cleaned_name and len(cleaned_name) > 2:
                    extracted_course_name = cleaned_name
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                if not table or len(table) < 2:
                    continue

                # Reset in_session_table if this table is clearly a non-session table
                first_row_text = " ".join(str(c).lower() for c in table[0] if c)
                if any(kw in first_row_text for kw in ["competency", "evaluation code", "sdg", "ers content", "faculty contact"]):
                    in_session_table = False

                # ── Step 1: look for the header row in this table ──────────
                local_mandatory_col = mandatory_col_idx
                local_session_col = session_col_idx
                data_start_row = 0

                for row_idx, row in enumerate(table):
                    # Merge cells vertically across next 3 rows to handle split headers
                    col_count = len(row)
                    merged_cells = [""] * col_count
                    for next_row_idx in range(row_idx, min(row_idx + 3, len(table))):
                        next_row = table[next_row_idx]
                        if not next_row or len(next_row) != col_count:
                            continue
                        for ci, cell in enumerate(next_row):
                            val = str(cell).replace("\n", " ").strip() if cell else ""
                            if val:
                                merged_cells[ci] += " " + val
                    
                    merged_cells = [c.strip().lower() for c in merged_cells]
                    # Normalize by removing spaces to handle split words like "mandator y"
                    merged_cells_norm = [c.replace(" ", "") for c in merged_cells]
                    
                    # Detect header row: a merged column must contain "mandatory" AND "session"
                    if any("mandatory" in c and "session" in c for c in merged_cells_norm):
                        for ci, cell in enumerate(merged_cells_norm):
                            if "mandatory" in cell and "session" in cell:
                                local_mandatory_col = ci
                                in_session_table = True
                                break
                        
                        # Dynamically detect which column contains the session numbers (usually col 0 or col 1)
                        # We count how many rows have a short session-like string with digits in col 0 vs col 1
                        col0_score = 0
                        col1_score = 0
                        for r in table[row_idx + 1:]:
                            if len(r) > 0:
                                val0 = str(r[0] or "").strip()
                                if val0 and re.search(r"\d", val0) and len(val0) < 20:
                                    col0_score += 1
                            if len(r) > 1:
                                val1 = str(r[1] or "").strip()
                                if val1 and re.search(r"\d", val1) and len(val1) < 20:
                                    col1_score += 1
                        
                        if col1_score > col0_score:
                            local_session_col = 1
                        else:
                            local_session_col = 0

                        # Store globally for subsequent pages
                        mandatory_col_idx = local_mandatory_col
                        session_col_idx = local_session_col
                        data_start_row = row_idx + 1
                        # Skip over sub-header/merged header rows (rows with no digit in the session col)
                        while data_start_row < len(table):
                            first_cell = str(table[data_start_row][local_session_col] or "").strip()
                            if re.search(r"\d", first_cell):
                                break
                            data_start_row += 1
                        break
                else:
                    # No header found — check if this looks like a continuation table
                    # (i.e., we already found the session table on a previous page)
                    if not in_session_table:
                        continue
                    # For continuation pages: use first col as session, last col as mandatory
                    local_session_col = session_col_idx if session_col_idx is not None else 0
                    local_mandatory_col = mandatory_col_idx if mandatory_col_idx is not None else (len(table[0]) - 1)
                    data_start_row = 0

                # ── Step 2: parse data rows ────────────────────────────────
                for row in table[data_start_row:]:
                    cells = [str(c).replace("\n", " ").strip() if c else "" for c in row]
                    if not cells:
                        continue

                    # Bounds check
                    if local_session_col >= len(cells) or local_mandatory_col >= len(cells):
                        # Try last column for mandatory
                        if len(cells) < 2:
                            continue
                        local_mandatory_col = len(cells) - 1
                        if local_session_col >= len(cells):
                            local_session_col = 0

                    session_cell = cells[local_session_col].strip()
                    
                    # Resolve mandatory cell by looking at the target column and adjacent columns to handle shifted cells
                    mandatory_cell = ""
                    candidates = [local_mandatory_col]
                    for offset in [1, -1, 2, -2]:
                        col_idx = local_mandatory_col + offset
                        if 0 <= col_idx < len(cells):
                            candidates.append(col_idx)
                    
                    for col_idx in candidates:
                        val = cells[col_idx].strip().lower()
                        # Only accept if it contains mandatory indicators/values to avoid false positives
                        if val and (any(kw in val for kw in ["yes", "*", "no", "n/a", "na"]) or val in {"y", "n"}):
                            mandatory_cell = val
                            break
                            
                    if not mandatory_cell and local_mandatory_col < len(cells):
                        mandatory_cell = cells[local_mandatory_col].strip().lower()

                    # Skip rows with no session number
                    if not re.search(r"\d", session_cell):
                        continue
                    
                    # Parse session numbers from the session cell
                    session_nums = _parse_session_nums(session_cell)
                    all_sessions_found.extend(session_nums)

                    # Skip rows with no mandatory indicator
                    if not mandatory_cell or mandatory_cell in {"-", "no", "n/a", "na"}:
                        continue
                    # Skip rows where mandatory cell has no "yes" or "*"
                    if "yes" not in mandatory_cell and "*" not in mandatory_cell:
                        continue

                    # Determine which sessions are mandatory
                    if mandatory_cell == "yes" or mandatory_cell == "*":
                        # All sessions in this row are mandatory
                        mandatory.extend(session_nums)
                    else:
                        # e.g. "session 9 - yes" → only session 9
                        specific = _parse_session_nums(mandatory_cell)
                        if specific:
                            mandatory.extend(specific)
                        else:
                            mandatory.extend(session_nums)

    # Deduplicate and sort
    mandatory = sorted(set(mandatory))
    all_sessions_found = sorted(set(all_sessions_found))
    return MandatorySessionInfo(
        course_code=course_code,
        course_shortname=course_shortname,
        mandatory_sessions=mandatory,
        all_sessions=all_sessions_found,
        course_name=extracted_course_name,
    )


def _parse_session_nums(text: str) -> list[int]:
    """Extract all integers from a string, expanding ranges like '1-2' or '7-9' or '14 to 16'."""
    nums = set()
    ranges = re.findall(r"(\d+)\s*(?:-|to)\s*(\d+)", text, re.IGNORECASE)
    for start, end in ranges:
        s, e = int(start), int(end)
        if s <= e:
            nums.update(range(s, e + 1))
        else:
            nums.update(range(e, s + 1))
    
    standalone = re.findall(r"\d+", text)
    for num_str in standalone:
        nums.add(int(num_str))
        
    return sorted(list(nums))
