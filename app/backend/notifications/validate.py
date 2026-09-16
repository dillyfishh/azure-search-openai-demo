"""Validate notification JSON or generate its editor schema without contacting Azure."""

import argparse
import json
from pathlib import Path

from pydantic import ValidationError

from .models import NotificationDocument


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, help="Notification JSON file (or schema output path)")
    parser.add_argument("--write-schema", action="store_true")
    args = parser.parse_args()
    if args.write_schema:
        schema = NotificationDocument.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        args.file.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
        return
    try:
        document = NotificationDocument.model_validate_json(args.file.read_bytes())
    except (OSError, ValidationError) as exc:
        parser.exit(1, f"Invalid notification document: {exc}\n")
    print(f"Valid: {len(document.notifications)} notification(s)")


if __name__ == "__main__":
    main()
