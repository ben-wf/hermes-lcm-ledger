"""Regression checks for Hermes Desktop's manifest and the runtime values it controls."""
from pathlib import Path

import pytest
import yaml

from hermes_lcm import config, db_bootstrap
from hermes_lcm.presets import get_preset, preset_env_diff


def test_manifest_covers_runtime_settings_and_matches_builtin_defaults():
    root = Path(__file__).resolve().parents[1]
    fields = yaml.safe_load((root / "plugin.yaml").read_text())["config_schema"]
    defaults = config.LCMConfig()
    assert fields.keys() == config.PROFILE_FIELD_TYPES.keys() | {
        "enable_slash_command", "fts_integrity_check_interval_hours",
    }
    assert fields["enable_slash_command"]["default"] is False
    assert fields["fts_integrity_check_interval_hours"]["default"] == db_bootstrap.DEFAULT_INTEGRITY_CHECK_INTERVAL_HOURS
    for name, kind in config.PROFILE_FIELD_TYPES.items():
        assert fields[name]["type"] == kind.__name__
        assert fields[name]["default"] == getattr(defaults, name)
        assert fields[name]["description"]
        assert fields[name]["label"]


def test_profile_settings_override_environment_and_track_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("LCM_FRESH_TAIL_COUNT", "53")
    monkeypatch.setenv("LCM_FTS_INTEGRITY_CHECK_INTERVAL_HOURS", "12")
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {"entries": {"hermes-lcm-ledger": {
            "config": {"fresh_tail_count": 17, "summary_model": "older"},
            "settings": {"fresh_tail_count": 9, "context_threshold": 0.42,
                         "rerank_enabled": False, "ignore_session_patterns": ["a,b", "cron-*"],
                         "recall_arm_weights": {"fts": 0}, "fts_integrity_check_interval_hours": 0},
        }}},
    }))
    actual = config.LCMConfig.from_env()
    assert actual.fresh_tail_count == 9
    assert actual.context_threshold == 0.42
    assert actual.config_sources["context_threshold"] == "plugin_settings"
    assert actual.summary_model == "older"
    assert actual.rerank_enabled is False
    assert actual.ignore_session_patterns == ["a,b", "cron-*"]
    assert actual.ignore_session_patterns_source == "plugin_settings"
    assert actual.recall_arm_weights == {"fts": 0, "summary": 1, "chunk": 1}
    assert db_bootstrap._integrity_check_interval_hours() == 0
    assert any("keep plugin setting 9" in row for row in preset_env_diff(get_preset(), actual))

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "other"))
    assert config.LCMConfig.from_env().fresh_tail_count == 53
    assert db_bootstrap._integrity_check_interval_hours() == 12


@pytest.mark.parametrize(("key", "value"), [
    ("fresh_tail_count", 3.5), ("rerank_enabled", "false"),
    ("context_threshold", float("nan")), ("ignore_session_patterns", [5]),
    ("recall_arm_weights", {"fts": -1}),
])
def test_invalid_saved_value_preserves_fallback_and_reports_key(key, value):
    before = config.LCMConfig()
    after = config.LCMConfig()
    after._apply_profile_settings({key: value})
    assert getattr(after, key) == getattr(before, key)
    assert after.config_source_warnings == [f"invalid plugin setting {key} ignored"]
