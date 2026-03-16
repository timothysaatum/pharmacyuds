import logging
import re

from docx import Document
import pandas as pd

from .models import ClassGroup, Voter

logger = logging.getLogger(__name__)

MAX_IMPORT_ROWS = 2000

# PHA/0003/20 format — letters, digits, / and -
VALID_VOTER_ID_RE = re.compile(r'^[A-Z0-9/_\-]+$')

# Expected header column names (lowercase, stripped)
HEADER_VOTER_ID = 'voter_id'
HEADER_NAME     = 'name'
HEADER_CONTACT  = 'contact'

# PDF backend preference: pdfplumber (table-aware) preferred over PyPDF2
# (line-based).  pdfplumber is optional — fall back gracefully if absent.
try:
    import pdfplumber
    _PDFPLUMBER_AVAILABLE = True
except ImportError:
    _PDFPLUMBER_AVAILABLE = False
    logger.info(
        "pdfplumber not installed — PDF table extraction will use PyPDF2. "
        "Install pdfplumber for more reliable PDF imports: pip install pdfplumber"
    )

try:
    import PyPDF2
    _PYPDF2_AVAILABLE = True
except ImportError:
    _PYPDF2_AVAILABLE = False


# ── Sanitisers ────────────────────────────────────────────────────────────────

def _sanitize_voter_id(value) -> str | None:
    if not value:
        return None
    value = str(value).strip().upper()
    if not value or len(value) < 3 or len(value) > 50:
        return None
    if not VALID_VOTER_ID_RE.match(value):
        logger.warning(f"Skipping invalid Voter ID: {value!r}")
        return None
    return value


def _sanitize_name(value) -> str:
    if not value:
        return ''
    return str(value).strip()[:255]


def _sanitize_phone(value) -> str:
    if not value:
        return ''
    # Keep only digits and +
    return re.sub(r'[^\d+]', '', str(value).strip())[:20]


# ── Word extractor ────────────────────────────────────────────────────────────

def extract_voters_from_word(file_path) -> list[dict]:
    """
    Parse the UDS School of Pharmacy class list Word document.

    Table columns:  S.No | name | voter_id | Contact
    The document contains split tables (the list continued across pages).
    Header rows are identified by checking if voter_id cell looks like a
    column label rather than an actual ID.
    """
    try:
        doc = Document(file_path)
    except Exception as e:
        logger.error(f"Cannot open Word file {file_path}: {e}")
        raise

    records = []
    header_values = {'voter_id', 'voteid', 'voter id', 's.no', 'sno', 'name', 'contact'}

    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if len(cells) < 3:
                continue

            # Skip header rows
            voter_id_cell = cells[2].lower()
            if voter_id_cell in header_values:
                continue

            voter_id = _sanitize_voter_id(cells[2])
            if not voter_id:
                continue

            name    = _sanitize_name(cells[1])
            contact = _sanitize_phone(cells[3]) if len(cells) > 3 else ''

            records.append({
                'matric_number': voter_id,
                'full_name':     name,
                'phone_number':  contact,
                'class_name':    'Level 600',
            })

            if len(records) >= MAX_IMPORT_ROWS:
                logger.warning("Word import hit MAX_IMPORT_ROWS limit")
                break

    logger.info(f"Extracted {len(records)} voters from Word document")
    return records


# ── Excel extractor ───────────────────────────────────────────────────────────

def extract_voters_from_excel(file_path) -> list[dict]:
    """
    Parse a voter Excel file.
    Expected columns: voter_id (or matric_number), name, contact, class_name (optional)
    """
    try:
        df = pd.read_excel(file_path, dtype=str)
    except Exception as e:
        logger.error(f"Cannot read Excel file {file_path}: {e}")
        raise

    # Normalize column names
    df.columns = [c.strip().lower().replace(' ', '_') for c in df.columns]

    # Accept voter_id or matric_number as the ID column
    id_col = None
    for candidate in ('voter_id', 'matric_number', 'matricnumber'):
        if candidate in df.columns:
            id_col = candidate
            break

    if not id_col:
        raise ValueError(
            f"Excel file must have a 'voter_id' or 'matric_number' column. "
            f"Found: {list(df.columns)}"
        )

    records = []
    for _, row in df.head(MAX_IMPORT_ROWS).iterrows():
        voter_id = _sanitize_voter_id(row.get(id_col))
        if not voter_id:
            continue

        name       = _sanitize_name(row.get('name', ''))
        contact    = _sanitize_phone(row.get('contact', ''))
        class_name = str(row.get('class_name', 'Level 600')).strip() or 'Level 600'

        records.append({
            'matric_number': voter_id,
            'full_name':     name,
            'phone_number':  contact,
            'class_name':    class_name,
        })

    logger.info(f"Extracted {len(records)} voters from Excel")
    return records


# ── PDF extractors ────────────────────────────────────────────────────────────

def _extract_pdf_pdfplumber(file_path) -> list[dict]:
    """
    Table-aware PDF extraction using pdfplumber.

    pdfplumber detects the actual table grid in the PDF and returns each
    cell as a discrete value, so it handles multi-column layouts, merged
    cells, and PDFs where text order is non-linear.  Much more robust than
    splitting raw text on commas or whitespace.

    Expected table structure: voter_id | name | contact  (any column order
    is handled by detecting a header row first).
    """
    records = []

    with pdfplumber.open(file_path) as pdf:
        # Attempt to find a column mapping from the first table header found
        col_map: dict[str, int] | None = None

        for page_num, page in enumerate(pdf.pages, start=1):
            tables = page.extract_tables()

            if not tables:
                # Page has no grid — try raw text as a line-delimited fallback
                text = page.extract_text() or ''
                for line in text.split('\n'):
                    parts = [p.strip() for p in re.split(r'[,\t|]', line)]
                    voter_id = _sanitize_voter_id(parts[0]) if parts else None
                    if not voter_id:
                        continue
                    name    = _sanitize_name(parts[1]) if len(parts) > 1 else ''
                    contact = _sanitize_phone(parts[2]) if len(parts) > 2 else ''
                    records.append({
                        'matric_number': voter_id,
                        'full_name':     name,
                        'phone_number':  contact,
                        'class_name':    'Level 600',
                    })
                continue

            for table in tables:
                if not table:
                    continue

                for row in table:
                    if not row:
                        continue

                    cells = [str(c).strip() if c else '' for c in row]

                    # Detect header row and build column map
                    lower = [c.lower() for c in cells]
                    if col_map is None and any(
                        h in lower for h in ('voter_id', 'voter id', 'name', 'contact')
                    ):
                        col_map = {}
                        for i, h in enumerate(lower):
                            if h in ('voter_id', 'voter id', 'matric', 'matric_number'):
                                col_map['voter_id'] = i
                            elif h == 'name':
                                col_map['name'] = i
                            elif h in ('contact', 'phone', 'phone_number'):
                                col_map['contact'] = i
                            elif h in ('class', 'class_name', 'level'):
                                col_map['class_name'] = i
                        continue  # skip the header row itself

                    # Extract using discovered column positions, or assume
                    # positional order [voter_id, name, contact] as fallback
                    if col_map:
                        id_idx      = col_map.get('voter_id', 0)
                        name_idx    = col_map.get('name', 1)
                        contact_idx = col_map.get('contact', 2)
                        class_idx   = col_map.get('class_name')
                    else:
                        id_idx, name_idx, contact_idx, class_idx = 0, 1, 2, None

                    voter_id = _sanitize_voter_id(
                        cells[id_idx] if id_idx < len(cells) else '')
                    if not voter_id:
                        continue

                    name    = _sanitize_name(cells[name_idx])    \
                              if name_idx < len(cells) else ''
                    contact = _sanitize_phone(cells[contact_idx]) \
                              if contact_idx < len(cells) else ''
                    cls     = (str(cells[class_idx]).strip()
                               if class_idx is not None and class_idx < len(cells)
                               else 'Level 600') or 'Level 600'

                    records.append({
                        'matric_number': voter_id,
                        'full_name':     name,
                        'phone_number':  contact,
                        'class_name':    cls,
                    })

                    if len(records) >= MAX_IMPORT_ROWS:
                        logger.warning("PDF (pdfplumber) import hit MAX_IMPORT_ROWS")
                        return records

    return records


def _extract_pdf_pypdf2(file_path) -> list[dict]:
    """
    Line-based PDF extraction using PyPDF2 — fallback when pdfplumber
    is not installed.

    NOTE: PyPDF2's text extraction is layout-unaware; lines may not
    correspond to table rows and multi-column PDFs often produce garbled
    output.  Install pdfplumber for production imports.
    """
    if not _PYPDF2_AVAILABLE:
        raise ImportError(
            "No PDF library available. Install pdfplumber: pip install pdfplumber"
        )

    try:
        reader = PyPDF2.PdfReader(file_path)
    except Exception as e:
        logger.error(f"Cannot read PDF {file_path}: {e}")
        raise

    text = ''
    for page in reader.pages:
        try:
            text += page.extract_text() or ''
        except Exception as e:
            logger.warning(f"PDF page extraction error: {e}")

    records = []
    for line in text.split('\n'):
        # Accept comma, tab, or pipe as delimiters
        parts = [p.strip() for p in re.split(r'[,\t|]', line)]
        voter_id = _sanitize_voter_id(parts[0]) if parts else None
        if not voter_id:
            continue

        name    = _sanitize_name(parts[1])    if len(parts) > 1 else ''
        contact = _sanitize_phone(parts[2])   if len(parts) > 2 else ''

        records.append({
            'matric_number': voter_id,
            'full_name':     name,
            'phone_number':  contact,
            'class_name':    'Level 600',
        })

        if len(records) >= MAX_IMPORT_ROWS:
            break

    return records


def extract_voters_from_pdf(file_path) -> list[dict]:
    """
    Public entry point: tries pdfplumber first (table-aware, reliable),
    then falls back to PyPDF2 if pdfplumber is not installed.
    """
    if _PDFPLUMBER_AVAILABLE:
        logger.info(f"Extracting PDF with pdfplumber: {file_path}")
        records = _extract_pdf_pdfplumber(file_path)
    else:
        logger.warning(
            f"Extracting PDF with PyPDF2 (install pdfplumber for better results): "
            f"{file_path}"
        )
        records = _extract_pdf_pypdf2(file_path)

    logger.info(f"Extracted {len(records)} voters from PDF")
    return records


# ── Persist voter records ─────────────────────────────────────────────────────

def save_voter_list(data: list[dict]) -> int:
    """
    Persist parsed voter records.
    - Idempotent: re-importing the same file is safe
    - Updates name/phone on existing voters if they were blank
    - Returns count of newly created voters
    """
    created = 0
    skipped = 0
    class_cache: dict[str, ClassGroup] = {}

    for entry in data:
        matric     = entry.get('matric_number')
        class_name = entry.get('class_name', 'Level 600') or 'Level 600'
        full_name  = entry.get('full_name', '')
        phone      = entry.get('phone_number', '')

        if not matric:
            skipped += 1
            continue

        try:
            if class_name not in class_cache:
                cg, _ = ClassGroup.objects.get_or_create(name=class_name)
                class_cache[class_name] = cg
            class_group = class_cache[class_name]

            voter, was_created = Voter.objects.get_or_create(
                matric_number=matric,
                defaults={
                    'class_group':  class_group,
                    'full_name':    full_name,
                    'phone_number': phone,
                },
            )

            if was_created:
                created += 1
            else:
                # Update blank fields on existing record without overwriting data
                updated_fields = []
                if not voter.full_name and full_name:
                    voter.full_name = full_name
                    updated_fields.append('full_name')
                if not voter.phone_number and phone:
                    voter.phone_number = phone
                    updated_fields.append('phone_number')
                if updated_fields:
                    voter.save(update_fields=updated_fields)
                skipped += 1

        except Exception as e:
            logger.error(f"Error saving voter {matric!r}: {e}")
            skipped += 1

    logger.info(f"Import done — created: {created}, skipped/existing: {skipped}")
    return created