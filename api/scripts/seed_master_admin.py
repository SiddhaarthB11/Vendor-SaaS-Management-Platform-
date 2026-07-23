"""Legacy master admin seed — delegates to env-based test user seeding."""

from app.test_users import configured_test_users


def main() -> None:
    users = configured_test_users()
    master = next((user for user in users if user["role"] == "master_admin"), None)
    if not master:
        print("No master admin configured. Set APP_USER_EMAIL_ADMIN and APP_USER_PASSWORD_ADMIN in infra/.env.")
        return
    if not master["password_hash"]:
        print("Master admin email is set but APP_USER_PASSWORD_ADMIN is missing.")
        return
    print(f"Master admin configured for {master['email']}. Run seed_login_users.py to apply all workflow test users.")


if __name__ == "__main__":
    main()
