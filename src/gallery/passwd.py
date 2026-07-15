"""Generate a users.yaml entry: uv run python -m gallery.passwd <username>"""

import getpass
import sys

from gallery.auth import hash_password


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python -m gallery.passwd <username>", file=sys.stderr)
        raise SystemExit(2)
    username = sys.argv[1]
    password = getpass.getpass(f"Password for {username}: ")
    if password != getpass.getpass("Repeat password: "):
        print("passwords do not match", file=sys.stderr)
        raise SystemExit(1)
    print(f"{username}: {hash_password(password)}")


if __name__ == "__main__":
    main()
