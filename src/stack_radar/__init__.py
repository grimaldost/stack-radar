"""stack-radar: a tech radar that governs a Claude Code stack, and the gate behind it.

The engine only. The catalogue it reads - entries, evidence, feedback, profiles - lives in
a separate directory called a DATA ROOT, found by the `radar.toml` marker at its top. One
engine serves any number of data roots and knows none of them by name.
"""

from __future__ import annotations

# A VERSION SITE. `radar_lib.version_site_errors` compares it with the anchor,
# `pyproject.toml [project].version`, alongside CHANGELOG.md's newest released heading,
# uv.lock's entry for the project and the install lines in README.md and docs/, and
# `tests/test_version_sites.py` fails when they disagree. It is written here rather than
# derived from the installed metadata because `importlib.metadata` answers for whatever
# copy is installed, which is the wrong question when a checkout is being judged, and
# answers nothing at all for the fallback that runs the package straight off a PYTHONPATH
# without installing it.
__version__ = "0.2.1"
