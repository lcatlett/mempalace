"""Fork-specific tests (3.3.3+lc.1): hook toggles + mempalace.yaml wing override.

Covers the three behavioral changes introduced in this fork:

1. ``hook_stop_auto_save`` / ``hook_precompact_auto_save`` config flags +
   env vars short-circuit the Stop and PreCompact hooks.
2. ``_wing_from_transcript_path`` honors a project-level ``mempalace.yaml``
   ``wing:`` field via the ``cwd`` parameter.
3. ``_parse_harness_input`` now extracts ``cwd`` from hook stdin JSON.
"""

from __future__ import annotations

import contextlib
import io
import json
from unittest.mock import MagicMock, PropertyMock, patch

import mempalace.hooks_cli as hooks_cli_mod
from mempalace.hooks_cli import (
    _log,
    _parse_harness_input,
    _wing_from_transcript_path,
    hook_precompact,
    hook_session_start,
    hook_stop,
)


# ---------------------------------------------------------------------------
# Hook toggle: hook_stop_auto_save / hook_precompact_auto_save
# ---------------------------------------------------------------------------


def _run_hook_with_toggles(
    hook_fn,
    data: dict,
    *,
    stop_auto_save: bool,
    precompact_auto_save: bool,
    state_dir,
):
    """Invoke a hook with explicit toggle states, capturing JSON output."""
    buf = io.StringIO()
    mock_config = MagicMock()
    type(mock_config).hook_silent_save = PropertyMock(return_value=True)
    type(mock_config).hook_desktop_toast = PropertyMock(return_value=False)
    type(mock_config).hook_stop_auto_save = PropertyMock(return_value=stop_auto_save)
    type(mock_config).hook_precompact_auto_save = PropertyMock(return_value=precompact_auto_save)
    patches = [
        patch(
            "mempalace.hooks_cli._output",
            side_effect=lambda d: buf.write(json.dumps(d)),
        ),
        patch("mempalace.hooks_cli.STATE_DIR", state_dir),
        patch("mempalace.config.MempalaceConfig", return_value=mock_config),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        hook_fn(data, "claude-code")
    return json.loads(buf.getvalue() or "{}")


def test_hook_stop_short_circuits_when_disabled(tmp_path):
    """With stop_auto_save=False, hook_stop must emit {} and skip all work."""
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")  # exists but empty — would normally not trigger save
    with (
        patch("mempalace.hooks_cli._count_human_messages") as mock_count,
        patch("mempalace.hooks_cli._save_diary_direct") as mock_save,
        patch("mempalace.hooks_cli._ingest_transcript") as mock_ingest,
    ):
        result = _run_hook_with_toggles(
            hook_stop,
            {
                "session_id": "test",
                "stop_hook_active": False,
                "transcript_path": str(transcript),
            },
            stop_auto_save=False,
            precompact_auto_save=True,
            state_dir=tmp_path,
        )
    assert result == {}
    # The short-circuit runs before any of these paths are touched.
    mock_count.assert_not_called()
    mock_save.assert_not_called()
    mock_ingest.assert_not_called()


def test_hook_stop_runs_normally_when_enabled(tmp_path):
    """With stop_auto_save=True (default), hook_stop reaches the count step."""
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    with patch("mempalace.hooks_cli._count_human_messages", return_value=0) as mock_count:
        _run_hook_with_toggles(
            hook_stop,
            {
                "session_id": "test",
                "stop_hook_active": False,
                "transcript_path": str(transcript),
            },
            stop_auto_save=True,
            precompact_auto_save=True,
            state_dir=tmp_path,
        )
    mock_count.assert_called_once()


def test_hook_precompact_short_circuits_when_disabled(tmp_path):
    """With precompact_auto_save=False, hook_precompact must emit {} and skip."""
    with (
        patch("mempalace.hooks_cli._ingest_transcript") as mock_ingest,
        patch("mempalace.hooks_cli._mine_sync") as mock_mine,
    ):
        result = _run_hook_with_toggles(
            hook_precompact,
            {"session_id": "test", "transcript_path": "/tmp/whatever.jsonl"},
            stop_auto_save=True,
            precompact_auto_save=False,
            state_dir=tmp_path,
        )
    assert result == {}
    mock_ingest.assert_not_called()
    mock_mine.assert_not_called()


def test_hook_precompact_runs_normally_when_enabled(tmp_path):
    """With precompact_auto_save=True, hook_precompact reaches the mine step."""
    with (
        patch("mempalace.hooks_cli._ingest_transcript") as mock_ingest,
        patch("mempalace.hooks_cli._mine_sync") as mock_mine,
    ):
        _run_hook_with_toggles(
            hook_precompact,
            {
                "session_id": "test",
                "transcript_path": "/tmp/some.jsonl",
            },
            stop_auto_save=True,
            precompact_auto_save=True,
            state_dir=tmp_path,
        )
    mock_ingest.assert_called_once()
    mock_mine.assert_called_once()


def test_hook_stop_env_var_overrides_config(tmp_path, monkeypatch):
    """MEMPALACE_HOOK_STOP_AUTO_SAVE=false short-circuits even when config says True."""
    monkeypatch.setenv("MEMPALACE_HOOK_STOP_AUTO_SAVE", "false")
    # Use the real MempalaceConfig — env var should win over file content.
    monkeypatch.setattr("mempalace.config.MempalaceConfig._config_file_path", None, raising=False)
    buf = io.StringIO()
    with (
        patch("mempalace.hooks_cli._output", side_effect=lambda d: buf.write(json.dumps(d))),
        patch("mempalace.hooks_cli.STATE_DIR", tmp_path),
        patch("mempalace.hooks_cli._count_human_messages") as mock_count,
    ):
        hook_stop(
            {"session_id": "test", "stop_hook_active": False, "transcript_path": ""},
            "claude-code",
        )
    assert json.loads(buf.getvalue() or "{}") == {}
    mock_count.assert_not_called()


# ---------------------------------------------------------------------------
# mempalace.yaml wing override (Change 3)
# ---------------------------------------------------------------------------


def test_wing_yaml_override_takes_precedence_over_path(tmp_path):
    """A project-level mempalace.yaml wing: field beats path-derived names."""
    (tmp_path / "mempalace.yaml").write_text("wing: meta_palace\n")
    transcript_path = "/Users/lcatlett/.claude/projects/-Users-lcatlett-projects-foo/session.jsonl"
    assert _wing_from_transcript_path(transcript_path, str(tmp_path)) == "meta_palace"


def test_wing_yaml_override_with_extra_fields(tmp_path):
    """Other fields in mempalace.yaml don't interfere with wing extraction."""
    (tmp_path / "mempalace.yaml").write_text(
        "wing: customer_engagements\nrooms:\n  - personal\n  - team_internal\n"
    )
    transcript_path = "/some/path/session.jsonl"
    assert _wing_from_transcript_path(transcript_path, str(tmp_path)) == "customer_engagements"


def test_wing_yaml_missing_falls_through_to_path(tmp_path):
    """No mempalace.yaml → existing path-derived behavior."""
    transcript_path = "/Users/lcatlett/.claude/projects/-home-jp-Projects-myproject/session.jsonl"
    assert _wing_from_transcript_path(transcript_path, str(tmp_path)) == "wing_myproject"


def test_wing_yaml_without_wing_field_falls_through(tmp_path):
    """mempalace.yaml exists but lacks `wing:` → fall through to path logic."""
    (tmp_path / "mempalace.yaml").write_text("rooms:\n  - personal\n")
    transcript_path = "/.claude/projects/-home-jp-Projects-myproj/x.jsonl"
    assert _wing_from_transcript_path(transcript_path, str(tmp_path)) == "wing_myproj"


def test_wing_yaml_malformed_fails_open(tmp_path):
    """Broken YAML must not lose data — fall through to path-derived wing."""
    (tmp_path / "mempalace.yaml").write_text("wing: [unclosed\n  bad: yaml")
    transcript_path = "/.claude/projects/-home-jp-Projects-myproj/x.jsonl"
    assert _wing_from_transcript_path(transcript_path, str(tmp_path)) == "wing_myproj"


def test_wing_function_signature_backward_compatible():
    """Existing callers passing only transcript_path must still work."""
    transcript_path = "/.claude/projects/-home-jp-Projects-foo/x.jsonl"
    assert _wing_from_transcript_path(transcript_path) == "wing_foo"


def test_wing_empty_cwd_falls_through(tmp_path):
    """cwd='' (default for non-Claude harnesses) → path-derived wing."""
    transcript_path = "/.claude/projects/-home-jp-Projects-foo/x.jsonl"
    assert _wing_from_transcript_path(transcript_path, "") == "wing_foo"


def test_wing_yaml_strips_whitespace(tmp_path):
    """wing: field is stripped before use."""
    (tmp_path / "mempalace.yaml").write_text("wing: '  decisions  '\n")
    assert _wing_from_transcript_path("/x/y/z.jsonl", str(tmp_path)) == "decisions"


# ---------------------------------------------------------------------------
# _parse_harness_input now extracts cwd
# ---------------------------------------------------------------------------


def test_parse_harness_input_extracts_cwd():
    result = _parse_harness_input(
        {
            "session_id": "abc",
            "stop_hook_active": False,
            "transcript_path": "/t.jsonl",
            "cwd": "/Users/lcatlett/projects/foo",
        },
        "claude-code",
    )
    assert result["cwd"] == "/Users/lcatlett/projects/foo"


def test_parse_harness_input_cwd_defaults_empty():
    result = _parse_harness_input(
        {"session_id": "abc", "stop_hook_active": False, "transcript_path": "/t.jsonl"},
        "claude-code",
    )
    assert result["cwd"] == ""


# ---------------------------------------------------------------------------
# Absent palace dir: hooks must not recreate ~/.mempalace/ (3.3.4+lc.1)
# ---------------------------------------------------------------------------


class TestHookAbsentDir:
    """When ~/.mempalace/ is absent, all hooks must return {} without touching disk."""

    _HOOK_DATA = {
        "session_id": "absent-test",
        "stop_hook_active": False,
        "transcript_path": "/dev/null",
        "cwd": "/tmp",
    }

    def _run_hook(self, hook_fn, tmp_path, monkeypatch):
        """Run hook_fn with PALACE_ROOT and STATE_DIR pointing at a non-existent subdir."""
        absent_root = tmp_path / ".mempalace"
        absent_state = absent_root / "hook_state"
        # Do NOT create absent_root — that's the whole point
        monkeypatch.setattr(hooks_cli_mod, "PALACE_ROOT", absent_root)
        monkeypatch.setattr(hooks_cli_mod, "STATE_DIR", absent_state)
        buf = io.StringIO()
        with patch("mempalace.hooks_cli._output", side_effect=lambda d: buf.write(json.dumps(d))):
            hook_fn(self._HOOK_DATA, "claude-code")
        return buf.getvalue(), absent_root

    def test_hook_stop_does_not_create_palace_dir(self, tmp_path, monkeypatch):
        output, absent_root = self._run_hook(hook_stop, tmp_path, monkeypatch)
        assert json.loads(output or "{}") == {}
        assert not absent_root.exists(), "hook_stop must not recreate ~/.mempalace/"

    def test_hook_precompact_does_not_create_palace_dir(self, tmp_path, monkeypatch):
        output, absent_root = self._run_hook(hook_precompact, tmp_path, monkeypatch)
        assert json.loads(output or "{}") == {}
        assert not absent_root.exists(), "hook_precompact must not recreate ~/.mempalace/"

    def test_hook_session_start_does_not_create_palace_dir(self, tmp_path, monkeypatch):
        output, absent_root = self._run_hook(hook_session_start, tmp_path, monkeypatch)
        assert json.loads(output or "{}") == {}
        assert not absent_root.exists(), "hook_session_start must not recreate ~/.mempalace/"

    def test_log_does_not_create_palace_dir_when_absent(self, tmp_path, monkeypatch):
        absent_root = tmp_path / ".mempalace"
        absent_state = absent_root / "hook_state"
        monkeypatch.setattr(hooks_cli_mod, "PALACE_ROOT", absent_root)
        monkeypatch.setattr(hooks_cli_mod, "STATE_DIR", absent_state)
        # Reset the initialized flag so _log would normally try to mkdir
        monkeypatch.setattr(hooks_cli_mod, "_state_dir_initialized", False)
        _log("should be silently dropped")
        assert not absent_root.exists(), "_log must not recreate ~/.mempalace/ when absent"

    def test_existing_dir_proceeds_normally(self, tmp_path, monkeypatch):
        """Regression: when ~/.mempalace/ exists, hook_stop still calls _count_human_messages."""
        present_root = tmp_path / ".mempalace"
        present_root.mkdir(parents=True)
        present_state = present_root / "hook_state"
        monkeypatch.setattr(hooks_cli_mod, "PALACE_ROOT", present_root)
        monkeypatch.setattr(hooks_cli_mod, "STATE_DIR", present_state)
        mock_config = MagicMock()
        type(mock_config).hook_stop_auto_save = PropertyMock(return_value=True)
        type(mock_config).hook_silent_save = PropertyMock(return_value=True)
        type(mock_config).hook_desktop_toast = PropertyMock(return_value=False)
        with (
            patch("mempalace.hooks_cli._output"),
            patch("mempalace.config.MempalaceConfig", return_value=mock_config),
            patch("mempalace.hooks_cli._count_human_messages", return_value=0) as mock_count,
        ):
            hook_stop(self._HOOK_DATA, "claude-code")
        mock_count.assert_called_once()
