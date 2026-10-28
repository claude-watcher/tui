"""Câblage TUI de `hide_sdk_sessions` (specs/005, 005-AC6) : flag CLI et interrupteur Settings."""

from pathlib import Path

import pytest


@pytest.mark.parametrize('config, argv, expected', [
    ('', [], True),
    ('', ['--show-sdk-sessions'], False),
    ('[features]\nhide_sdk_sessions = false\n', [], False),
    ('[features]\nhide_sdk_sessions = true\n', ['--show-sdk-sessions'], False),
])
def test_show_sdk_sessions_turns_the_setting_off_for_the_run(watcher, monkeypatch, tmp_path,
                                                            config, argv, expected):
    """005-AC6 : `--show-sdk-sessions` l'éteint pour ce run, sans toucher au config.ini."""
    path = tmp_path / 'config.ini'
    path.write_text(config)
    monkeypatch.setattr(watcher, 'CONFIG_PATH', path)
    assert watcher.parse_args(watcher.load_config(), argv).hide_sdk_sessions is expected
    assert path.read_text() == config


def test_the_settings_switch_is_wired_like_hide_daemons(watcher):
    """005-AC6 : textual est stubé (classes = MagicMock) : contrôle sur la SOURCE."""
    src = Path(watcher.__file__).read_text()
    assert """Switch(value=getattr(CFG, 'hide_sdk_sessions', True), id="cfg-sdk")""" in src
    branch = src.split('elif event.switch.id == "cfg-sdk":', 1)[1].split('elif ', 1)[0]
    assert 'CFG.hide_sdk_sessions = val' in branch
    assert "save_config({'features': {'hide_sdk_sessions': 'true' if val else 'false'}})" in branch
    assert {'cfg_sdk', 'cfg_sdk_d'} <= watcher.STRINGS['fr'].keys() & watcher.STRINGS['en'].keys()
