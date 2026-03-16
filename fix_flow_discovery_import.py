"""
fix_flow_discovery_import.py
Run from D:\\website_qa_tool: python fix_flow_discovery_import.py
"""
from pathlib import Path

f = Path("engines/flow_discovery.py")
src = f.read_text(encoding="utf-8")

# Remove misplaced 'import os' from the top if present
if src.startswith("import os\n"):
    src = src[len("import os\n"):]
    print("  Removed import os from top")

# Ensure 'from __future__ import annotations' is first
if not src.startswith("from __future__ import annotations"):
    print("  WARNING: __future__ import not at top — check file manually")
else:
    print("  __future__ import is correctly at top")

# Ensure 'import os' exists somewhere after the __future__ line
if "import os\n" not in src:
    src = src.replace(
        "from __future__ import annotations\n",
        "from __future__ import annotations\nimport os\n",
        1
    )
    print("  Added import os after __future__ import")
else:
    print("  import os already present")

f.write_text(src, encoding="utf-8")
print("\nDone — restart your RQ worker.")