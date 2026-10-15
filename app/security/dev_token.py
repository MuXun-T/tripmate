from app.config import get_settings
from app.persistence.postgres.seed import seed_id
from app.security.auth import issue_bearer_token


def main() -> None:
    settings = get_settings()
    token = issue_bearer_token(
        seed_id("tenant:demo"),
        seed_id("user:demo"),
        settings.auth_signing_secret.get_secret_value(),
    )
    print(token)


if __name__ == "__main__":
    main()
