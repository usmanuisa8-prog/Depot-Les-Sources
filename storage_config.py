from pathlib import Path, PurePosixPath
import os
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _configured_path(variable, default):
    value = os.environ.get(variable)
    path = Path(value).expanduser() if value else Path(default).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    return path.resolve()


DATABASE_PATH = _configured_path("DATABASE_PATH", BASE_DIR / "depot.db")
STORAGE_ROOT = _configured_path("STORAGE_ROOT", BASE_DIR)
INVOICE_STORAGE_DIR = STORAGE_ROOT / "invoice_storage"
GENERATED_INVOICES_DIR = STORAGE_ROOT / "generated_invoices"


def resolve_invoice_pdf_path(stored_path):
    """Resolve current and legacy Windows/Linux invoice PDF paths."""
    if not stored_path:
        return None

    raw = str(stored_path).strip()
    if not raw:
        return None

    # Try the exact stored path first, for local compatibility.
    direct = Path(raw).expanduser()
    if direct.is_file():
        return direct.resolve()

    # PurePosixPath handles legacy Windows paths after separator conversion.
    normalized = raw.replace("\\", "/")
    filename = PurePosixPath(normalized).name
    if not filename or filename in {".", ".."}:
        return None

    lowered = normalized.lower()
    if "generated_invoices" in lowered:
        preferred = [GENERATED_INVOICES_DIR, INVOICE_STORAGE_DIR]
    elif "invoice_storage" in lowered:
        preferred = [INVOICE_STORAGE_DIR, GENERATED_INVOICES_DIR]
    else:
        preferred = [INVOICE_STORAGE_DIR, GENERATED_INVOICES_DIR]

    for directory in preferred:
        candidate = directory / filename
        if candidate.is_file():
            return candidate.resolve()

    return None
