"""Entry point: `python -m sls` starts the desktop app; subcommands for administration.

    python -m sls                 start the application
    python -m sls backup          create a database backup now
    python -m sls list-backups    list available backups
    python -m sls restore FILE    restore the database from FILE (current DB is preserved)
    python -m sls create-admin USERNAME   create an admin account (prompts for password)
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from .core.settings import load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sls", description="Survey & Licensing Studio")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("backup")
    sub.add_parser("list-backups")
    r = sub.add_parser("restore")
    r.add_argument("file", type=Path)
    a = sub.add_parser("create-admin")
    a.add_argument("username")
    args = parser.parse_args(argv)
    settings = load_settings()
    if args.cmd is None:
        from .ui.app import run

        return run(settings)
    from .persistence.backup import list_backups, restore_backup
    from .persistence.workspace import DB_FILENAME, Workspace

    if args.cmd == "restore":
        db_path = settings.data_path / DB_FILENAME
        preserved = restore_backup(args.file, db_path)
        print(f"Restored {args.file} -> {db_path}\nPrevious database preserved as {preserved}")
        return 0
    if args.cmd == "list-backups":
        for p in list_backups(settings.data_path / "backups"):
            print(p)
        return 0
    ws = Workspace(settings, auto_backup=False)
    try:
        if args.cmd == "backup":
            print(ws.backup_now("cli"))
        elif args.cmd == "create-admin":
            pw = getpass.getpass("Password (min 8 chars): ")
            ws.users.create(args.username, pw, "admin")
            print(f"Admin user '{args.username}' created.")
    finally:
        ws.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
