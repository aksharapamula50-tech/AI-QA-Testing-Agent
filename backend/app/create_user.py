"""Create a local QA workspace account without putting passwords in shell history."""
from getpass import getpass
import sys

from app.main import create_user, init_db


def main() -> int:
    email = input("Account email: ").strip()
    password = getpass("Password (at least 12 characters): ")
    confirmation = getpass("Confirm password: ")
    if password != confirmation:
        print("Passwords do not match.", file=sys.stderr)
        return 1
    init_db()
    try:
        user = create_user(email, password)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"Created account for {user['email']}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
