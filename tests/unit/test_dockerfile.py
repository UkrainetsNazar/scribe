from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile"


def test_the_image_runs_as_an_unprivileged_user() -> None:
    users = [
        line.split()[1]
        for line in (stripped.strip() for stripped in DOCKERFILE.read_text().splitlines())
        if line.startswith("USER ")
    ]
    assert users and users[-1] not in {"root", "0"}
