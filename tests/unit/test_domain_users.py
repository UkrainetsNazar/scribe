from scribe.domain.users import Plan, User, normalize_email

PLAN = Plan(id=1, name="free", monthly_minutes=300, max_file_mb=500)


def test_normalize_email() -> None:
    assert normalize_email("  User@Example.COM ") == "user@example.com"


def test_register_normalizes_email_and_sets_defaults() -> None:
    user = User.register("Foo@Example.com", "hash", PLAN)
    assert user.email == "foo@example.com"
    assert user.is_active is True
    assert user.plan == PLAN
