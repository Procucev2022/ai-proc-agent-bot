"""
MySQL connections must carry socket timeouts. Without a read timeout, a query whose
reply is lost on the network leaves the chat request waiting forever.
"""

from types import SimpleNamespace

import pytest

from app import config as config_mod
from app import database


def test_mysql_urls_get_socket_timeouts_and_keep_ssl():
    settings = SimpleNamespace(db_connect_timeout_seconds=5, db_read_timeout_seconds=45, db_write_timeout_seconds=50)
    ssl_args = {"ssl": {"ca": "/app/ssl/ca.pem"}}

    result = database._with_socket_timeouts("mysql+pymysql://u:p@host/db", ssl_args, settings)

    assert result == {"ssl": {"ca": "/app/ssl/ca.pem"}, "connect_timeout": 5, "read_timeout": 45, "write_timeout": 50}
    assert ssl_args == {"ssl": {"ca": "/app/ssl/ca.pem"}}  # input is not mutated


def test_defaults_apply_when_settings_lack_timeouts():
    result = database._with_socket_timeouts("mysql+pymysql://u:p@host/db", {}, SimpleNamespace())
    assert result == {"connect_timeout": 10, "read_timeout": 60, "write_timeout": 60}


def test_non_pymysql_urls_are_left_unchanged():
    args = {"check_same_thread": False}
    assert database._with_socket_timeouts("sqlite:///test.db", args, SimpleNamespace()) is args


def test_settings_read_timeouts_from_environment(monkeypatch):
    monkeypatch.setenv("DB_CONNECT_TIMEOUT_SECONDS", "3")
    monkeypatch.setenv("DB_READ_TIMEOUT_SECONDS", "20")
    monkeypatch.setenv("DB_WRITE_TIMEOUT_SECONDS", "25")
    settings = config_mod.Settings()
    assert (settings.db_connect_timeout_seconds, settings.db_read_timeout_seconds, settings.db_write_timeout_seconds) == (3, 20, 25)


def test_validate_config_rejects_non_positive_db_timeouts(monkeypatch):
    monkeypatch.setattr(config_mod, "load_dotenv", lambda: None)
    settings = config_mod.Settings()
    for attr, env_name in (
        ("db_connect_timeout_seconds", "DB_CONNECT_TIMEOUT_SECONDS"),
        ("db_read_timeout_seconds", "DB_READ_TIMEOUT_SECONDS"),
        ("db_write_timeout_seconds", "DB_WRITE_TIMEOUT_SECONDS"),
    ):
        original = getattr(settings, attr)
        setattr(settings, attr, 0)
        with pytest.raises(ValueError, match=env_name):
            settings.validate_config()
        setattr(settings, attr, original)
