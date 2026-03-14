import pytest
from pathlib import Path
from isis_monitor.config import load_config

def test_load_config_success(tmp_path):
    config_file = tmp_path / "config.ini"
    config_file.write_text("""
[DATA]
mcr_news_url = http://test.com/news
isis_websocket_url = wss://test.com/ws

[WEBHOOKS]
news_teams_url = http://test.teams/news
beam_teams_url = http://test.teams/beam
experiment_teams_url = http://test.teams/exp
""")
    config = load_config(config_file)
    assert config.mcr_news_url == "http://test.com/news"
    assert config.isis_websocket_url == "wss://test.com/ws"
    assert config.news_teams_url == "http://test.teams/news"
    assert config.beam_teams_url == "http://test.teams/beam"
    assert config.experiment_teams_url == "http://test.teams/exp"

def test_load_config_missing_file():
    with pytest.raises(SystemExit):
        load_config(Path("non_existent_file.ini"))

def test_load_config_missing_mcr_url(tmp_path):
    config_file = tmp_path / "config.ini"
    config_file.write_text("""
[DATA]
isis_websocket_url = wss://test.com/ws
""")
    with pytest.raises(SystemExit):
        load_config(config_file)
