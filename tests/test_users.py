import os

import pytest

import dbstore
import users
from users import UserError


def test_normalize():
    assert users.normalize("  Alice@Example.COM ") == "alice@example.com"
    assert users.normalize(None) == ""


@pytest.mark.parametrize("name", ["", "-lead", ".lead", "has space", "a/b", "../x", "x" * 65, "ünï"])
def test_create_rejects_bad_usernames(name):
    with pytest.raises(UserError):
        users.create(name, "longenough")


@pytest.mark.parametrize("name", ["a", "bob.smith", "a@b.co", "x" * 64, "u_1-2"])
def test_create_accepts_good_usernames(name):
    assert users.create(name, "longenough") == name


def test_create_lowercases_and_makes_folder():
    users.create("Bob", "longenough")
    assert users.get("bob")
    assert os.path.isdir(dbstore.user_dir("bob"))


def test_create_rejects_short_password_and_duplicates():
    with pytest.raises(UserError):
        users.create("bob", "short")
    users.create("bob", "longenough")
    with pytest.raises(UserError):
        users.create("BOB", "longenough")


def test_first_user_is_admin_and_clears_setup_code():
    code = users.setup_code()
    assert users.check_setup_code(code)
    users.create("first", "longenough", admin=False)
    assert users.get("first")["admin"] is True
    assert not users.check_setup_code(code)
    users.create("second", "longenough")
    assert users.get("second")["admin"] is False


def test_setup_code_is_stable_and_checked_strictly():
    code = users.setup_code()
    assert users.setup_code() == code
    assert users.check_setup_code(f"  {code} ")
    assert not users.check_setup_code("")
    assert not users.check_setup_code(None)
    assert not users.check_setup_code(code + "x")


def test_password_hash_is_not_plaintext():
    users.create("bob", "longenough")
    assert "longenough" not in users.get("bob")["password_hash"]


def test_verify():
    users.create("bob", "longenough")
    assert users.verify("BOB", "longenough")
    assert users.verify("bob", "wrong") is None
    assert users.verify("nobody", "longenough") is None


def test_proxy_user_without_password_cannot_log_in():
    users.create("admin", "longenough")
    users.create("proxied", None)
    assert users.get("proxied")["password_hash"] == ""
    assert users.verify("proxied", "") is None


def test_set_password_bumps_session_version():
    users.create("bob", "longenough")
    before = users.get("bob")["session_version"]
    users.set_password("bob", "another-pass")
    assert users.get("bob")["session_version"] == before + 1
    assert users.verify("bob", "another-pass")
    assert users.verify("bob", "longenough") is None
    with pytest.raises(UserError):
        users.set_password("bob", "short")


def test_cannot_demote_or_remove_last_admin():
    users.create("root", "longenough")
    with pytest.raises(UserError):
        users.set_admin("root", False)
    with pytest.raises(UserError):
        users.remove("root")
    users.create("other", "longenough", admin=True)
    users.set_admin("root", False)
    assert users.get("root")["admin"] is False


def test_remove_moves_data_aside():
    users.create("root", "longenough")
    users.create("bob", "longenough")
    with open(os.path.join(dbstore.user_dir("bob"), "config.json"), "w") as f:
        f.write("{}")
    users.remove("bob")
    assert users.get("bob") is None
    assert not os.path.exists(dbstore.user_dir("bob"))
    moved = os.listdir(os.path.join(dbstore.USERS_DIR, ".removed"))
    assert len(moved) == 1 and moved[0].startswith("bob-")


def test_remove_unknown_user():
    users.create("root", "longenough")
    with pytest.raises(UserError):
        users.remove("ghost")


# -- login throttling ---------------------------------------------------------

def test_user_lockout_after_max_failures():
    for _ in range(users.MAX_FAILURES["user"] - 1):
        users.record_failure("1.1.1.1", "bob")
    assert users.locked_out("9.9.9.9", "bob") == 0
    users.record_failure("1.1.1.1", "bob")
    wait = users.locked_out("9.9.9.9", "BOB")  # other IP, name normalized
    assert 0 < wait <= users.LOCKOUT_SECONDS + 1
    assert users.locked_out("9.9.9.9", "carol") == 0


def test_ip_lockout_covers_all_names():
    for i in range(users.MAX_FAILURES["ip"]):
        users.record_failure("1.1.1.1", f"user{i}")
    assert users.locked_out("1.1.1.1", "fresh") > 0
    assert users.locked_out("2.2.2.2", "fresh") == 0


def test_failures_expire(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(users.time, "time", lambda: now[0])
    for _ in range(users.MAX_FAILURES["user"]):
        users.record_failure("1.1.1.1", "bob")
    assert users.locked_out("1.1.1.1", "bob") > 0
    now[0] += users.LOCKOUT_SECONDS + 1
    assert users.locked_out("1.1.1.1", "bob") == 0


def test_clear_failures():
    for _ in range(users.MAX_FAILURES["user"]):
        users.record_failure("1.1.1.1", "bob")
    users.clear_failures("1.1.1.1", "bob")
    assert users.locked_out("1.1.1.1", "bob") == 0
