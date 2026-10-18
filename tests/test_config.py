from zypher_agent.config import Settings, load_icp


def test_allowlist_parses_comma_separated(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "111, 222")
    assert Settings(_env_file=None).telegram_allowed_user_ids == [111, 222]


def test_allowlist_empty_by_default(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)
    assert Settings(_env_file=None).telegram_allowed_user_ids == []


def test_secrets_do_not_print(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    assert "sk-secret" not in repr(Settings(_env_file=None))


def test_icp_loads():
    icp = load_icp(Settings(_env_file=None).icp_path)
    assert icp.messaging_rules.email_word_range == (50, 80)
    assert "revolutionary" in icp.messaging_rules.never_say
    assert any(p.id == "usr_first_deployment" for p in icp.proof_points)
