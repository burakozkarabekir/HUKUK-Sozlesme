"""Command-line tools.

Usage:
    python cli.py migrate          # apply DB migrations
    python cli.py stats            # row counts and schema version
    python cli.py sync             # (phase 3)
    python cli.py reindex          # (phase 3)
    python cli.py check-llm        # (phase 2)
    python cli.py check-turkuaz    # (phase 6)
"""

from __future__ import annotations

import argparse
import sys

from app.config import get_settings
from app.db import connect, current_version, migrate, table_counts


def cmd_migrate(_: argparse.Namespace) -> int:
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        applied = migrate(conn)
        version = current_version(conn)
    finally:
        conn.close()
    if applied:
        print(f"Uygulanan migration'lar: {applied}. Şema sürümü: {version}")
    else:
        print(f"Veritabanı güncel. Şema sürümü: {version}")
    print(f"Veritabanı: {settings.db_path}")
    return 0


def cmd_stats(_: argparse.Namespace) -> int:
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        print(f"Veritabanı : {settings.db_path}")
        print(f"Şema sürümü: {current_version(conn)}")
        for table, count in table_counts(conn).items():
            print(f"  {table:<16} {count}")
    finally:
        conn.close()
    return 0


def _not_yet(phase: int):
    def handler(args: argparse.Namespace) -> int:
        print(f"'{args.command}' komutu Faz {phase}'te eklenecek.", file=sys.stderr)
        return 2

    return handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sözleşme Mevzuat Etki Analizi - yönetim komutları")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="Veritabanı migration'larını uygula").set_defaults(func=cmd_migrate)
    sub.add_parser("stats", help="Kayıt sayılarını göster").set_defaults(func=cmd_stats)
    sub.add_parser("sync", help="Sözleşmeleri senkronize et").set_defaults(func=_not_yet(3))
    sub.add_parser("reindex", help="İndeksi yeniden oluştur").set_defaults(func=_not_yet(3))
    sub.add_parser("check-llm", help="LLM bağlantısını test et").set_defaults(func=_not_yet(2))
    sub.add_parser("check-turkuaz", help="Turkuaz endpoint'ini test et").set_defaults(func=_not_yet(6))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
