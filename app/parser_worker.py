"""One request per process: secret input is carried over stdin, never argv."""
import base64
import json
import logging
import sys

from .parser import ParseError, parse_pdf


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    try:
        payload = json.load(sys.stdin)
        result = parse_pdf(base64.b64decode(payload["pdf"]), payload["password"])
        json.dump({"result": result}, sys.stdout)
    except ParseError as exc:
        json.dump({"error": str(exc)}, sys.stdout)
    except Exception:
        json.dump({"error": "Unable to process this PDF."}, sys.stdout)
