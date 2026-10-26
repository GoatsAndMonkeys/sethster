"""`python -m sethster_server.accounts`: account admin from the server's shell (docs/plans/accounts.md).

  create-admin <username> [--display-name N]   new approved admin (password prompted), or promote an existing user
  create-user <username> [--display-name N] [--approved]   e.g. the App Review accounts
  reset-password <username>                    prints a one-time password and signs the user out everywhere
  list
  delete <username> [--yes]                    the user, sessions, push devices and Rotation
  move-rotation <listener> <username>          e.g. `move-rotation default alice` at cutover (backs up rotation.db)

Uses the same SETHSTER_DATA / SETHSTER_ACCOUNTS_DB / SETHSTER_ROTATION_DB as the server. Password prompts can be
fed on stdin with --password-stdin (one line).
"""
from __future__ import annotations

import argparse
import getpass
import secrets
import sys

from . import accounts_store as acc
from .accounts_api import accounts_db_path
from .accounts_store import AccountError, AccountsStore
from .config import load_settings
from .rotation_store import RotationStore, rotation_db_path


def _password(args) -> str:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\n")
    pw = getpass.getpass("Password: ")
    if getpass.getpass("Again: ") != pw:
        raise SystemExit("passwords differ")
    return pw


def _user(store: AccountsStore, username: str):
    u = store.user_by_name(username)
    if u is None:
        raise SystemExit(f"no user {username!r}")
    return u


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sethster_server.accounts", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("create-admin", "create-user"):
        p = sub.add_parser(name)
        p.add_argument("username")
        p.add_argument("--display-name")
        p.add_argument("--password-stdin", action="store_true")
        if name == "create-user":
            p.add_argument("--approved", action="store_true")
    sub.add_parser("reset-password").add_argument("username")
    sub.add_parser("list")
    p = sub.add_parser("delete")
    p.add_argument("username")
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("move-rotation")
    p.add_argument("source", help="the listener to move from, e.g. default")
    p.add_argument("username", help="the account that receives it")
    args = ap.parse_args(argv)

    settings = load_settings()
    store = AccountsStore(accounts_db_path(settings))
    rotation = RotationStore(rotation_db_path(settings))

    try:
        if args.cmd in ("create-admin", "create-user"):
            admin = args.cmd == "create-admin"
            existing = store.user_by_name(args.username)
            if existing is not None:
                if not admin:
                    raise SystemExit(f"user {args.username!r} already exists")
                store.set_status(existing["id"], "approved", None)
                store.make_admin(existing["id"], None)
                print(f"promoted {existing['username']} (#{existing['id']}) to approved admin")
                return 0
            u = store.create_user(args.username, _password(args), args.display_name,
                                  status="approved" if admin or args.approved else "pending", is_admin=admin)
            print(f"created {u['username']} (#{u['id']}) {u['status']}{' admin' if admin else ''}")
        elif args.cmd == "reset-password":
            u = _user(store, args.username)
            pw = secrets.token_urlsafe(12)
            n = store.set_password(u["id"], pw)
            print(f"one-time password for {u['username']}: {pw}\n({n} session(s) revoked; "
                  f"ask them to change it in Settings → Account)")
        elif args.cmd == "list":
            print(f"{'id':>4}  {'username':<20} {'status':<9} {'admin':<5} {'created':<20} display name")
            for u in store.users():
                print(f"{u['id']:>4}  {u['username']:<20} {u['status']:<9} {'yes' if u['is_admin'] else '':<5} "
                      f"{u['created_at']:<20} {u['display_name']}")
        elif args.cmd == "delete":
            u = _user(store, args.username)
            if not args.yes and input(f"delete {u['username']} (#{u['id']}) and their Rotation? [y/N] ").lower() != "y":
                return 1
            store.delete_user(u["id"])  # before the Rotation, as DELETE /auth/me does
            n = rotation.clear(acc.listener(u["id"]))
            print(f"deleted {u['username']} (#{u['id']}), {n} Rotation item(s)")
            if store.admin_count() == 0:
                print("warning: no approved admin left; run create-admin", file=sys.stderr)
        elif args.cmd == "move-rotation":
            u = _user(store, args.username)
            dest = acc.listener(u["id"])
            if args.source == dest:
                raise SystemExit("source and destination are the same")
            before = rotation.counts(args.source, dest)
            path = rotation.path
            backup = path.with_name(f"{path.name}.bak-{acc.now().strftime('%Y%m%d-%H%M%S')}")
            if path.exists():
                rotation.backup(backup)
                print(f"backed up {path} -> {backup}")
            res = rotation.move(args.source, dest)
            after = rotation.counts(args.source, dest)
            print(f"before: {args.source}={before[args.source]} {dest}={before[dest]}")
            print(f"moved {res['moved']}, skipped {res['skipped']} already there")
            print(f"after:  {args.source}={after[args.source]} {dest}={after[dest]}")
    except AccountError as e:
        raise SystemExit(f"{e.code}: {e.detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
