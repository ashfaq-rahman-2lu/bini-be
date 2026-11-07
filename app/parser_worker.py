"""One request per process: secret input is carried over stdin, never argv."""
import base64
import json
import logging
import sys

from .parser import ParseError, parse_pdf
from .pdf_tools import unlock_pdf


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    try:
        payload = json.load(sys.stdin)
        data = base64.b64decode(payload["pdf"])
        if payload.get("operation") == "unlock":
            result = base64.b64encode(unlock_pdf(data, payload["password"])).decode()
        else:
            result = parse_pdf(data, payload["password"])
        json.dump({"result": result}, sys.stdout)
    except ParseError as exc:
        json.dump({"error": str(exc)}, sys.stdout)
    except Exception:
        json.dump({"error": "Unable to process this PDF."}, sys.stdout)
