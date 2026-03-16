# patch_test_case_generator.py
# Run from D:\website_qa_tool: python patch_test_case_generator.py

from pathlib import Path
import shutil

TARGET = Path("engines/test_case_generator.py")
if not TARGET.exists():
    print(f"ERROR: {TARGET} not found")
    exit(1)

src = TARGET.read_text(encoding="utf-8")
shutil.copy(TARGET, TARGET.with_suffix(".py.bak"))

# Fix _build_test_steps to include page_url in each step dict
old = """        test_steps.append(TestStep(
            step_number=s["step_number"],
            description=detail,
            action=action,
            target=target,
            value=value,
        ))"""

new = """        test_steps.append(TestStep(
            step_number=s["step_number"],
            description=detail,
            action=action,
            target=target,
            value=value,
            page_url=s.get("page_url", ""),   # destination URL for nav fallback
        ))"""

if old in src:
    src = src.replace(old, new, 1)
    print("  FIXED: TestStep now carries page_url")
else:
    print("  SKIP: target already patched or not found")

# Fix TestStep dataclass to include page_url field
old2 = """@dataclass
class TestStep:
    step_number: int
    description: str
    action: str          # navigate|fill|click|submit|assert|wait
    target: Optional[str] = None   # CSS selector or URL
    value: Optional[str] = None    # value to type (fill action)"""

new2 = """@dataclass
class TestStep:
    step_number: int
    description: str
    action: str          # navigate|fill|click|submit|assert|wait
    target: Optional[str] = None   # CSS selector or URL
    value: Optional[str] = None    # value to type (fill action)
    page_url: Optional[str] = None # destination URL (used as nav fallback)"""

if old2 in src:
    src = src.replace(old2, new2, 1)
    print("  FIXED: TestStep dataclass has page_url field")
else:
    print("  SKIP: TestStep already has page_url or not found")

# Fix to_dict() to include page_url
old3 = """            "steps": [
                {
                    "step_number": s.step_number,
                    "description": s.description,
                    "action":      s.action,
                    "target":      s.target,
                    "value":       s.value,
                }
                for s in self.steps
            ],"""

new3 = """            "steps": [
                {
                    "step_number": s.step_number,
                    "description": s.description,
                    "action":      s.action,
                    "target":      s.target,
                    "value":       s.value,
                    "page_url":    getattr(s, "page_url", ""),
                }
                for s in self.steps
            ],"""

if old3 in src:
    src = src.replace(old3, new3, 1)
    print("  FIXED: to_dict() includes page_url")
else:
    print("  SKIP: to_dict() already includes page_url")

TARGET.write_text(src, encoding="utf-8")
print(f"\nDone. Backup at {TARGET.with_suffix('.py.bak')}")