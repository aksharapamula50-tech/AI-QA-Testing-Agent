
from getpass import getpass
from app.main import db, hash_password, init_db

email = input("Enter your existing account email: ").strip().lower()
password = getpass("Enter your new password (minimum 12 characters): ")
confirm = getpass("Confirm your new password: ")

if password != confirm:
    print("Passwords do not match.")
    raise SystemExit(1)

if len(password) < 12 or len(password) > 256:
    print("Password must be between 12 and 256 characters.")
    raise SystemExit(1)

init_db()

with db() as conn:
    cursor = conn.execute(
        "UPDATE users SET password_hash = ? WHERE email = ?",
        (hash_password(password), email)
    )

    if cursor.rowcount == 0:
        print("No account found with that email. No password was changed.")
    else:
        print("Password reset successful! You can log in now.")
