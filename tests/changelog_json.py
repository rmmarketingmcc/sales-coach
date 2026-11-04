#!/usr/bin/env python3
"""CHANGELOG.md to docs/changelog.json, which the guide page reads for its version and "What's new".
release.sh runs it on every release."""
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
text = (REPO / "CHANGELOG.md").read_text()
out = [{"version": m.group(1), "date": m.group(2),
        "notes": [line[2:].strip() for line in m.group(3).splitlines() if line.startswith("- ")]}
       for m in re.finditer(r"^## (\S+) \(([^)]*)\)\n(.*?)(?=^## |\Z)", text, re.M | re.S)]
(REPO / "docs" / "changelog.json").write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
print(f"docs/changelog.json: {len(out)} versions, newest {out[0]['version'] if out else '-'}")
