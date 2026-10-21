"""`.env.example` is the checklist for a new machine (laptop or server): it must name every setting, once."""
import re
from collections import Counter

from zypher_agent.config import REPO_ROOT, Settings

# "KEY=value" or a commented-out "# KEY=value" (optional settings are listed commented out).
LINE = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)=", re.MULTILINE)


def _keys() -> list[str]:
    return LINE.findall((REPO_ROOT / ".env.example").read_text(encoding="utf-8"))


def test_no_key_is_listed_twice():
    # A second SMTP_PORT=587 further down once silently overrode the right value for anyone copying the file.
    assert [k for k, n in Counter(_keys()).items() if n > 1] == []


def test_every_setting_the_code_reads_is_listed():
    listed = set(_keys())
    assert [f.upper() for f in Settings.model_fields if f.upper() not in listed] == []
