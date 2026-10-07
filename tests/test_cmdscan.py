"""Command-scanner tests.

Every case here is a mistake that a simpler parser makes, so each gets its own assertion.
"""

from __future__ import annotations

import pytest
from stack_radar.cmdscan import executables, resolve, tokenize


class TestEnvironmentAssignments:
    def test_posix_assignment_is_not_the_executable(self):
        # A whitespace-splitting parser reads the assignment's value as the command.
        chain, _ = executables('F="C:/path/x" git -C "$F" status')
        assert chain == ["git"]

    def test_multiple_assignments(self):
        chain, _ = executables("A=1 B=2 PYTHONUTF8=1 uv run pytest")
        assert chain == ["uv", "pytest"]

    def test_powershell_assignment_yields_nothing(self):
        chain, _ = executables('$d = "C:/x"')
        assert chain == []

    def test_powershell_assignment_then_command(self):
        chain, _ = executables("$d='C:/x'; uv run ty check")
        assert chain == ["uv", "ty"]


class TestRunners:
    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("uv run ruff check", ["uv", "ruff"]),
            ("npx widget", ["npx", "widget"]),
            ("python -m pytest", ["python", "pytest"]),
            ("uv run python -m pytest tests/ -q", ["uv", "python", "pytest"]),
            ("uv run --python 3.14 --no-project python -m mypy", ["uv", "python", "mypy"]),
            ("uv tool run ruff check", ["uv", "ruff"]),
            ("uvx widget check", ["uvx", "widget"]),
            ("uv run -m pytest", ["uv", "python", "pytest"]),
            ("poetry run pytest", ["poetry", "pytest"]),
            ("pipx run black .", ["pipx", "black"]),
            ("wsl uv run pytest", ["wsl", "uv", "pytest"]),
            ("xargs -n1 uv run ruff check", ["xargs", "uv", "ruff"]),
            ("time uv run pytest", ["uv", "pytest"]),
        ],
    )
    def test_runner_resolution(self, command, expected):
        chain, _ = executables(command)
        assert chain == expected

    def test_uv_with_value_flag_does_not_swallow_the_command(self):
        chain, _ = executables("uv run --with pypdf python extract.py")
        assert chain == ["uv", "python"]

    def test_bare_runner_without_a_subcommand(self):
        chain, _ = executables("uv --version")
        assert chain == ["uv"]

    def test_script_path_resolves_to_python(self):
        chain, _ = executables("uv run analyse.py")
        assert chain == ["uv", "python"]


class TestSegmentSplitting:
    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("a | b", ["a", "b"]),
            ("a && b", ["a", "b"]),
            ("a || b", ["a", "b"]),
            ("a ; b", ["a", "b"]),
            ("a\nb", ["a", "b"]),
            ("echo $(git rev-parse HEAD)", ["echo", "git"]),
            ("echo `date`", ["echo", "date"]),
        ],
    )
    def test_separators(self, command, expected):
        chain, _ = executables(command)
        assert chain == expected

    def test_substring_matching_is_wrong(self):
        # "grep ruff docs/*.md" is not a ruff invocation.
        chain, _ = executables("grep ruff docs/*.md")
        assert chain == ["grep"]
        assert "ruff" not in chain

    def test_pipeline_head_of_each_segment(self):
        chain, _ = executables("ls | grep ty | head -3")
        assert chain == ["ls", "grep", "head"]
        assert "ty" not in chain

    def test_loop_keywords_do_not_become_executables(self):
        chain, _ = executables("for f in *.py; do uv run ruff check $f; done")
        assert chain == ["uv", "ruff"]

    def test_conditional_keywords(self):
        chain, _ = executables("if [ -f x ]; then uv run pytest; fi")
        assert "uv" in chain and "pytest" in chain
        assert "then" not in chain and "fi" not in chain


class TestQuotingAndHeredocs:
    def test_quoted_text_is_not_a_command(self):
        chain, _ = executables('echo "uv run pytest" > note.txt')
        assert chain == ["echo"]

    def test_python_dash_c_body_is_opaque(self):
        # Newline-splitting this body yields "import", "d=json.load(...)" and
        # "print(mypy)" as phantom commands.
        chain, _ = executables("python -c \"\nimport json\nd=json.load(open('x'))\nprint(mypy)\n\"")
        assert chain == ["python"]

    def test_a_shell_wrapper_body_is_opaque(self):
        # The blind spot `field_report.command_identities` documents, pinned here so the
        # documented behaviour and the code cannot drift apart. Same class as `python
        # -c`: the inner command line is one quoted argument, so a `cmd:` count is a
        # FLOOR rather than a measurement. Opening those quotes would mean re-parsing an
        # arbitrary nested language, which is how a newline-splitting scanner invents
        # invocations out of heredoc bodies.
        for command in (
            'powershell -NoProfile -Command "uv run ruff check ."',
            'pwsh -c "uv run pytest -q"',
            'bash -c "uv run ruff check"',
        ):
            chain, _ = executables(command)
            assert len(chain) == 1, command
            assert "uv" not in chain and "ruff" not in chain and "pytest" not in chain

    def test_heredoc_body_is_opaque(self):
        command = "cat <<'EOF' > f.py\nimport pytest\nassert ruff\nEOF\nuv run pytest"
        chain, _ = executables(command)
        assert chain == ["cat", "uv", "pytest"]
        assert "assert" not in chain and "import" not in chain

    def test_unquoted_heredoc_delimiter(self):
        chain, _ = executables("cat <<PY\nmypy is mentioned here\nPY\necho done")
        assert chain == ["cat", "echo"]
        assert "mypy" not in chain

    def test_comment_is_ignored(self):
        chain, _ = executables("echo hi; # uv run pytest")
        assert chain == ["echo"]

    def test_tokenize_keeps_quoted_span_as_one_token(self):
        segments = tokenize('git commit -m "a; b | c"')
        assert segments == [["git", "commit", "-m", "a; b | c"]]


class TestNormalisation:
    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("./.venv/Scripts/ruff.exe check", "ruff"),
            ('"/c/Program Files/Python313/python" x.py', "python"),
            ("C:/Users/{user}/AppData/Roaming/npm/widget.cmd report --json", "widget"),
            (r"C:\tools\gh.exe pr list", "gh"),
        ],
    )
    def test_paths_reduce_to_basenames(self, command, expected):
        chain, _ = executables(command)
        assert chain == [expected]

    def test_redirection_target_is_not_a_command(self):
        chain, _ = executables("uv run pytest > out.txt 2>&1")
        assert chain == ["uv", "pytest"]

    def test_empty_command(self):
        assert executables("") == ([], [])

    def test_unbalanced_quote_degrades_without_raising(self):
        chain, _ = executables('echo "unterminated')
        assert chain == ["echo"]

    def test_resolve_is_depth_limited(self):
        # Pathological nesting must terminate rather than recurse forever.
        assert resolve(["uv", "run"] * 40) == ["uv"] * 7

    def test_windows_backslash_paths_survive_tokenising(self):
        assert tokenize(r"C:\tools\gh.exe pr list")[0][0] == r"C:\tools\gh.exe"

    def test_backslash_still_escapes_shell_metacharacters(self):
        chain, _ = executables(r"find . -name '*.tmp' -exec rm {} \; && echo done")
        assert chain == ["find", "echo"]
