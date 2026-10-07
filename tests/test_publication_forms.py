"""The publication check's two FORM rules: no home-finding outside bootstrap, no real
account in a `C:/Users/<segment>` literal.

These are the halves of the check that name nothing private, so they are tested here on
planted source rather than through a catalogue - and then held against this repository's
own tree, which is the one tree every run of this suite can see.

The home-finding rule reads the PARSED module, because a text match on `Path.home()`
would fire on every comment, docstring, message and test that names the call; the
negatives below are those cases.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from stack_radar.gate import PATH_HOME_EXEMPT, _user_literal_hits, home_finding_calls

REPO = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ home-finding calls

POSITIVES = {
    "Path.home()": "from pathlib import Path\nhome = Path.home()\n",
    "pathlib.Path.home()": "import pathlib\nhome = pathlib.Path.home()\n",
    "os.path.expanduser() on a literal": 'import os\nh = os.path.expanduser("~/x")\n',
    "bare expanduser() on a literal": 'from os.path import expanduser\nh = expanduser("~")\n',
    "Path('~').expanduser()": 'from pathlib import Path\nh = Path("~/.claude").expanduser()\n',
    "an f-string that starts with ~": 'from pathlib import Path\nh = Path(f"~/{x}").expanduser()\n',
    "os.environ['USERPROFILE']": 'import os\nh = os.environ["USERPROFILE"]\n',
    "os.environ.get('HOME')": 'import os\nh = os.environ.get("HOME", "")\n',
    "os.getenv('HOME')": 'import os\nh = os.getenv("HOME")\n',
    "getenv imported bare": 'from os import getenv\nh = getenv("USERPROFILE")\n',
    "environ imported bare": 'from os import environ\nh = environ["HOMEPATH"]\n',
    "Path.home passed uncalled": "from pathlib import Path\nf = field(default_factory=Path.home)\n",
    "a name bound to a ~ literal": 'D = "~/.claude"\nh = Path(D).expanduser()\n',
    "os.path.expanduser on a ~ name": 'D = "~"\nh = os.path.expanduser(D)\n',
    "an aliased Path class": 'from pathlib import Path as P\nh = P("~").expanduser()\n',
    "an aliased Path.home()": "from pathlib import Path as P\nh = P.home()\n",
    "an aliased os.path": 'import os.path as osp\nh = osp.expanduser("~")\n',
    "an aliased getenv": 'from os import getenv as ge\nh = ge("HOME")\n',
    "a concatenation that starts with ~": 'import os\nh = os.path.expanduser("~" + "/x")\n',
    "a name annotated and bound to ~": 'D: str = "~/x"\nh = Path(D).expanduser()\n',
    "os.environ.setdefault('HOME')": 'import os\nh = os.environ.setdefault("HOME", "/h")\n',
    "os.environ.pop('USERPROFILE')": 'import os\nh = os.environ.pop("USERPROFILE", None)\n',
}

NEGATIVES = {
    "a comment": "# Path.home() is only ever called in bootstrap\nx = 1\n",
    "a docstring": '"""Only bootstrap calls `Path.home()` or reads os.environ["HOME"]."""\n',
    "a message string": 'MSG = "calls `Path.home()` - only bootstrap.py may"\n',
    "a test patching the attribute": (
        'monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake))\n'
    ),
    "expanduser on a value the operator typed": (
        "from pathlib import Path\nroot = Path(str(given)).expanduser()\n"
    ),
    "os.path.expanduser on a variable": "import os\np = os.path.expanduser(value)\n",
    "a subprocess handed its own home": 'env["HOME"] = env["USERPROFILE"] = str(home)\n',
    "an assignment to os.environ": 'import os\nos.environ["HOME"] = "/tmp/h"\n',
    "another variable": 'import os\nroot = os.environ.get("RADAR_DATA_ROOT")\n',
    "a variable read by name": "import os\nv = os.environ.get(NAME)\n",
    "a different uncalled .home": "f = field(default_factory=team.home)\n",
    "a name bound to something else": 'D = "docs"\nh = Path(D).expanduser()\n',
    "a different .home attribute": "team.home()\n",
}


@pytest.mark.parametrize("source", POSITIVES.values(), ids=POSITIVES.keys())
def test_a_real_home_finding_call_is_found(source):
    hits = home_finding_calls(source)
    assert len(hits) == 1, hits
    assert hits[0][0] == 2  # the line of the call, not of the import


@pytest.mark.parametrize("source", NEGATIVES.values(), ids=NEGATIVES.keys())
def test_a_mention_or_a_user_given_path_is_not(source):
    assert home_finding_calls(source) == []


def test_a_file_that_does_not_parse_falls_back_to_the_text_rule():
    # Skipping an unparseable file would pass exactly the file nobody can read.
    hits = home_finding_calls("def broken(:\n    return Path.home()\n")
    assert [line for line, _ in hits] == [2]
    assert "does not parse" in hits[0][1]


# ------------------------------------------------------------------ account literals

# Built by concatenation, so this file carries no `C:/Users/<name>` literal itself and
# stays clean under the rule it tests.
USERS = "C:" + "/Users/"


@pytest.mark.parametrize(
    "segment",
    [
        "{user}",
        "<you>",
        "%USERNAME%",
        "$USER",
        "${USER}",
        "user",
        "username",
        "you",
        "example",
        "Public",
        "Default",
        "Default User",
        "All Users",
        "all users",
    ],
)
def test_a_placeholder_segment_is_not_an_account(segment):
    assert _user_literal_hits(f"under {USERS}{segment}/Documents") == []
    assert _user_literal_hits(f'"{USERS}{segment}"') == []


def test_a_folder_name_is_not_a_prefix_that_hides_an_account():
    # `All Users` is read as one segment only when the segment ends there. An account
    # whose name merely starts with it is still an account.
    literal = USERS + "Allison"
    assert _user_literal_hits(f"mine lives at {literal}/Documents") == [literal]


def test_a_segment_nobody_declared_is_an_account():
    literal = USERS + "someone"
    assert _user_literal_hits(f"mine lives at {literal}/Documents") == [literal]


# ------------------------------------------------------------------ this repository


def tracked_python() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "*.py"], cwd=REPO, capture_output=True, check=True
    ).stdout
    return [REPO / p for p in out.decode("utf-8").split("\0") if p]


def test_this_tree_passes_its_own_form_rules():
    files = tracked_python()
    assert any(f.name == "gate.py" for f in files), "git listed none of this tree's modules"
    found = []
    for f in files:
        text = f.read_text(encoding="utf-8")
        if f.name != PATH_HOME_EXEMPT:
            found += [f"{f.name}:{line}: {what}" for line, what in home_finding_calls(text)]
        found += [f"{f.name}: {hit}" for hit in _user_literal_hits(text)]
    assert found == [], found
