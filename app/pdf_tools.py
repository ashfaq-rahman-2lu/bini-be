"""PDF utilities operate on in-memory bytes, without ledger persistence."""
import io

from pypdf import PdfReader, PdfWriter

from .parser import ParseError


def unlock_pdf(data: bytes, password: str = "") -> bytes:
    if len(data) > 10 * 1024 * 1024:
        raise ParseError("PDF must be at most 10 MB.")
    if not data.startswith(b"%PDF-"):
        raise ParseError("Upload a valid PDF file.")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(password):
            raise ParseError("Incorrect PDF password.")
        if not 0 < len(reader.pages) <= 50:
            raise ParseError("PDF must contain between 1 and 50 pages.")
        writer = PdfWriter()
        # Clone the document tree to retain metadata, outlines, and form fields.
        writer.clone_document_from_reader(reader)
        output = io.BytesIO()
        writer.write(output)
        return output.getvalue()
    except ParseError:
        raise
    except Exception:
        raise ParseError("Unable to unlock this PDF. Check its password and format.") from None
