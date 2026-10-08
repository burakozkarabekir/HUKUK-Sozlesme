"""Command-line tools.

Usage:
    python cli.py migrate          # apply DB migrations
    python cli.py stats            # row counts and schema version
    python cli.py sync [--full]    # fetch + index contracts (incremental)
    python cli.py reindex          # re-chunk stored PDFs, rebuild FTS
    python cli.py embed            # create missing embeddings (vector search)
    python cli.py check-llm        # LLM connectivity + supported features
    python cli.py check-turkuaz    # Turkuaz connectivity + raw field names [--out x.json]
    python cli.py hash-password    # bcrypt hash for BASIC_AUTH_USERS
    python cli.py demo-data        # sample contract PDFs for local testing
    python cli.py backup [--out f] # consistent online DB backup
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.config import Settings, apply_network_env, get_settings
from app.db import connect, current_version, migrate, table_counts
from app.errors import AppError
from app.llm import prompts
from app.llm.client import LLMClient
from app.llm.schemas import CITY_PROBE


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


def _mask(value: str) -> str:
    return "(boş)" if not value else f"…{value[-4:]} ({len(value)} karakter)"


async def _check_step(title: str, step: Callable[[], Awaitable[str]]) -> bool:
    print(f"\n▶ {title}")
    started = time.perf_counter()
    try:
        detail = await step()
    except AppError as exc:
        print(f"  ❌ {exc.message} [{exc.code}]")
        return False
    except Exception as exc:  # report anything (SSL, DNS...) instead of a traceback
        print(f"  ❌ {type(exc).__name__}: {exc}")
        return False
    print(f"  ✅ {detail}  ({(time.perf_counter() - started) * 1000:.0f} ms)")
    return True


async def run_check_llm(settings: Settings, client: LLMClient | None = None) -> int:
    print("Yapay zeka (Azure AI Foundry) bağlantı kontrolü")
    print(f"  Base URL : {settings.AZURE_OPENAI_BASE_URL or '(boş)'}")
    print(f"  Model    : {settings.CHAT_MODEL}")
    print(f"  API key  : {_mask(settings.AZURE_OPENAI_API_KEY.get_secret_value())}")
    print(f"  Proxy    : {settings.HTTPS_PROXY or '(yok)'}")
    print(f"  CA       : {settings.ca_bundle_path or '(sistem varsayılanı)'}")

    client = client or LLMClient(settings)
    try:
        client.ensure_configured()
    except AppError as exc:
        print(f"\n❌ {exc.message}")
        return 1
    try:
        return await _run_llm_checks(client)
    finally:
        await client.aclose()


async def _run_llm_checks(client: LLMClient) -> int:

    async def simple() -> str:
        result = await client.complete(prompts.BASE_RULES, prompts.CHECK_SIMPLE, purpose="check")
        return f"Cevap: {result.text.strip()[:60]!r}  token: {result.usage.total_tokens}"

    stream_chunks: list[str] = []

    async def streaming() -> str:
        stream = client.stream_text(prompts.BASE_RULES, prompts.CHECK_STREAM, purpose="check", allow_fallback=False)
        async for chunk in stream:
            stream_chunks.append(chunk)
        if len(stream_chunks) < 2:
            raise AppError("stream_single_chunk", "Cevap tek parça geldi; streaming fiilen çalışmıyor.")
        return f"{len(stream_chunks)} parça alındı"

    async def structured(mode: str) -> str:
        parsed, result = await client.generate_json(
            prompts.BASE_RULES, prompts.CHECK_JSON, CITY_PROBE, purpose="check", mode=mode
        )
        return f"{parsed.model_dump()}  token: {result.usage.total_tokens}"

    ok_simple = await _check_step("1/4 Basit çağrı (Responses API)", simple)
    if not ok_simple:
        print("\nTemel çağrı başarısız; diğer adımlar atlandı. Sorun giderme için README'ye bakın.")
        return 1
    ok_stream = await _check_step("2/4 Streaming", streaming)
    ok_schema = await _check_step("3/4 Yapılandırılmış çıktı (json_schema)", lambda: structured("json_schema"))
    ok_prompt = await _check_step("4/4 Prompt ile JSON (yedek yöntem)", lambda: structured("prompt"))

    print("\nÖnerilen .env ayarları:")
    print(f"  LLM_STREAMING={'on' if ok_stream else 'off'}")
    if ok_schema:
        print("  LLM_STRUCTURED_MODE=json_schema")
    elif ok_prompt:
        print("  LLM_STRUCTURED_MODE=prompt")
    else:
        print("  ⚠️  Yapılandırılmış çıktı hiçbir yöntemle alınamadı; etki analizi çalışmaz.")
    return 0 if (ok_schema or ok_prompt) else 1


def cmd_check_llm(_: argparse.Namespace) -> int:
    settings = get_settings()
    apply_network_env(settings)
    return asyncio.run(run_check_llm(settings))


def cmd_sync(args: argparse.Namespace) -> int:
    from app.ingest.pipeline import SyncAlreadyRunning, run_sync

    settings = get_settings()
    apply_network_env(settings)
    try:
        stats = run_sync(settings, trigger=args.trigger, triggered_by="cli", full=args.full)
    except SyncAlreadyRunning as exc:
        print(f"⚠️  {exc.message}", file=sys.stderr)
        return 3
    print(f"Senkronizasyon #{stats.run_id}: {stats.status}")
    print(f"  Yeni: {stats.new}  Güncellenen: {stats.updated}  Değişmeyen: {stats.unchanged}  Silinen: {stats.deleted}")
    for error in stats.errors[:20]:
        print(f"  ❌ {error['contract_id'] or '-'}: {error['message']}")
    if len(stats.errors) > 20:
        print(f"  ... ve {len(stats.errors) - 20} hata daha")
    return 0 if stats.status == "success" else 1


def cmd_reindex(_: argparse.Namespace) -> int:
    from app.ingest.pipeline import SyncAlreadyRunning, reindex

    settings = get_settings()
    apply_network_env(settings)
    try:
        counts = reindex(settings)
    except SyncAlreadyRunning as exc:
        print(f"⚠️  {exc.message}", file=sys.stderr)
        return 3
    print(f"Yeniden indekslendi: {counts['files']} dosya, {counts['chunks']} madde, {counts['errors']} hata")
    return 0 if counts["errors"] == 0 else 1


def cmd_embed(_: argparse.Namespace) -> int:
    settings = get_settings()
    apply_network_env(settings)
    if not settings.vector_search_enabled:
        print("EMBEDDING_MODEL tanımlı değil; vektör arama kapalı.", file=sys.stderr)
        return 2
    from app.services.embeddings import embed_missing, make_sync_embedder

    conn = connect(settings.db_path)
    try:
        migrate(conn)
        count = embed_missing(conn, settings, make_sync_embedder(settings))
    except AppError as exc:
        print(f"❌ {exc.message}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"{count} madde için embedding üretildi.")
    return 0


def _short(value: object, limit: int = 80) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value
    if isinstance(value, str) and len(value) > 200 and " " not in value[:200]:
        return f"<{len(value)} karakterlik veri (base64 olabilir)>"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _flatten(value: object, prefix: str = "") -> list[tuple[str, object]]:
    """Field paths in mapping syntax: a.b, items[].name."""
    rows: list[tuple[str, object]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            rows.extend(_flatten(item, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, list) and value and isinstance(value[0], dict):
        rows.extend(_flatten(value[0], f"{prefix}[]"))
    else:
        rows.append((prefix, value))
    return rows


def _truncate_blobs(value: object) -> object:
    if isinstance(value, dict):
        return {k: _truncate_blobs(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate_blobs(v) for v in value]
    if isinstance(value, str) and len(value) > 300:
        return value[:60] + f"… <{len(value)} karakter>"
    return value


def _print_fields(record: dict) -> None:
    for path, value in _flatten(record):
        print(f"    {path:<40} {type(value).__name__:<6} {_short(value)}")


def run_check_turkuaz(settings: Settings, source: object | None = None, out: str | None = None) -> int:
    from app.sources import turkuaz_mapping as mapping
    from app.sources.base import SourceError
    from app.sources.turkuaz_source import TurkuazSource, missing_settings

    print("Turkuaz bağlantı kontrolü")
    print(f"  Base URL   : {settings.TURKUAZ_BASE_URL or '(boş)'}")
    print(f"  Auth modu  : {settings.TURKUAZ_AUTH_MODE}")
    print(f"  Proxy      : {settings.HTTPS_PROXY or '(yok)'}   SSL doğrulama: {settings.TURKUAZ_VERIFY_SSL}")
    print(f"  Liste yolu : {mapping.LIST_PATH}   Sayfalama: {mapping.PAGINATION['style']}")
    missing = missing_settings(settings)
    if missing and source is None:
        print(f"\n❌ Eksik ayarlar: {', '.join(missing)}")
        return 1
    dump: dict[str, object] = {}
    try:
        turkuaz = source or TurkuazSource(settings)
        print("\n▶ 1/3 Sözleşme listesi (ilk sayfa)")
        body, records = turkuaz.raw_first_page(3)  # type: ignore[attr-defined]
        if isinstance(body, dict):
            print(f"  Yanıtın üst düzey alanları: {', '.join(body.keys())}")
        print(f"  Gelen kayıt (ilk {len(records)} gösteriliyor)")
        dump["list_response_keys"] = list(body.keys()) if isinstance(body, dict) else type(body).__name__
        dump["records"] = _truncate_blobs(records)
        if not records:
            print("  ⚠️  Kayıt yok. PAGINATION['items_path'] doğru mu?")
            return 1
        for index, record in enumerate(records, start=1):
            print(f"\n  Kayıt {index} alanları (eşleştirme yolu / tip / örnek değer):")
            _print_fields(record)

        print("\n▶ 2/3 Alan eşleştirmesi (turkuaz_mapping.CONTRACT_FIELD_MAP)")
        first_meta = None
        for index, record in enumerate(records, start=1):
            try:
                meta = mapping.map_contract(record)
            except ValueError as exc:
                print(f"  ❌ Kayıt {index} eşleştirilemedi: {str(exc).splitlines()[0]}")
                continue
            first_meta = first_meta or meta
            empty = [f for f, p in mapping.CONTRACT_FIELD_MAP.items() if mapping.get_path(record, p) in (None, "", [])]
            print(f"  ✅ Kayıt {index}: id={meta.contract_id} no={meta.contract_no} taraflar={meta.parties}")
            if empty:
                print(f"     ⚠️  Boş/eşleşmeyen alanlar: {', '.join(empty)}")
            if meta.extra:
                print(f"     ℹ️  Eşleştirilmemiş alanlar (extra): {', '.join(meta.extra)}")
        if first_meta is None:
            print("  ❌ Hiçbir kayıt eşleştirilemedi; CONTRACT_FIELD_MAP güncellenmeli.")
            return 1

        print(f"\n▶ 3/3 Dosyalar ({mapping.DOCUMENTS_PATH.format(contract_id=first_meta.contract_id)})")
        raw_docs = turkuaz.get_json(  # type: ignore[attr-defined]
            mapping.DOCUMENTS_PATH.format(contract_id=first_meta.contract_id)
        )
        items = mapping.get_path(raw_docs, mapping.DOCUMENTS_ITEMS_PATH) if mapping.DOCUMENTS_ITEMS_PATH else raw_docs
        dump["documents"] = _truncate_blobs(raw_docs)
        if isinstance(raw_docs, dict):
            print(f"  Yanıtın üst düzey alanları: {', '.join(raw_docs.keys())}")
        if not isinstance(items, list) or not items:
            print("  ⚠️  Dosya listesi bulunamadı. DOCUMENTS_ITEMS_PATH doğru mu?")
            return 1
        print("  İlk dosyanın alanları:")
        _print_fields(items[0])
        files = turkuaz.get_contract_document(first_meta.contract_id)  # type: ignore[attr-defined]
        for file in files:
            size = len(file.read_bytes())
            kind = "Ek" if file.is_attachment else "Ana"
            print(f"  ✅ {file.file_name} [{kind}] {size:,} bayt, PDF: {file.read_bytes()[:4] == b'%PDF'}")
    except SourceError as exc:
        print(f"\n❌ {exc.message} [{exc.code}]")
        return 1
    finally:
        if out:
            with open(out, "w", encoding="utf-8") as handle:
                json.dump(dump, handle, ensure_ascii=False, indent=2, default=str)
            print(f"\nHam örnekler kaydedildi: {out}")
    print("\n✅ Turkuaz bağlantısı ve eşleştirme çalışıyor.")
    return 0


def cmd_hash_password(_: argparse.Namespace) -> int:
    import getpass

    from app.auth.basic import hash_password

    username = input("Kullanıcı adı: ").strip()
    password = getpass.getpass("Şifre: ")
    if not username or ":" in username or "," in username or len(password) < 10:
        print("Kullanıcı adı ':' veya ',' içeremez; şifre en az 10 karakter olmalı.", file=sys.stderr)
        return 2
    if getpass.getpass("Şifre (tekrar): ") != password:
        print("Şifreler eşleşmiyor.", file=sys.stderr)
        return 2
    print("\n.env içindeki BASIC_AUTH_USERS değerine ekleyin (tek tırnak içinde, virgülle ayırarak):")
    print(f"{username}:{hash_password(password)}")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    """Consistent online copy of the database (safe while the app is running)."""
    import sqlite3
    from datetime import datetime

    settings = get_settings()
    target = Path(args.out) if args.out else settings.DATA_DIR / "backups" / f"app-{datetime.now():%Y%m%d-%H%M%S}.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    source = connect(settings.db_path)
    try:
        destination = sqlite3.connect(target)
        with destination:
            source.backup(destination)
        destination.close()
    finally:
        source.close()
    print(f"Yedek alındı: {target}")
    print(f"Not: PDF dosyaları ayrıca yedeklenmeli: {settings.DATA_DIR / 'files'}")
    return 0


def cmd_demo_data(args: argparse.Namespace) -> int:
    settings = get_settings()
    target = Path(args.dir) if args.dir else settings.LOCAL_CONTRACTS_DIR
    try:
        from app.demo import write_demo_contracts

        created = write_demo_contracts(target)
    except ImportError:
        print("reportlab gerekli: pip install -r requirements-dev.txt", file=sys.stderr)
        return 2
    print(f"{len(created)} örnek PDF oluşturuldu: {target}")
    print("Örnek mevzuat metni: ornek_mevzuat_degisikligi.txt")
    print("Sonraki adım: python cli.py sync")
    return 0


def cmd_check_turkuaz(args: argparse.Namespace) -> int:
    settings = get_settings()
    apply_network_env(settings)
    return run_check_turkuaz(settings, out=args.out)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sözleşme Mevzuat Etki Analizi - yönetim komutları")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="Veritabanı migration'larını uygula").set_defaults(func=cmd_migrate)
    sub.add_parser("stats", help="Kayıt sayılarını göster").set_defaults(func=cmd_stats)
    sync = sub.add_parser("sync", help="Sözleşmeleri kaynaktan çek ve indeksle (incremental)")
    sync.add_argument("--full", action="store_true", help="Tümünü tara; kaynakta olmayanları sil")
    sync.add_argument("--trigger", default="cli", choices=["cli", "timer"], help=argparse.SUPPRESS)
    sync.set_defaults(func=cmd_sync)
    sub.add_parser("reindex", help="Saklanan PDF'lerden maddeleri ve FTS indeksini yeniden oluştur").set_defaults(
        func=cmd_reindex
    )
    sub.add_parser("embed", help="Embedding'i eksik maddeler için vektör üret (EMBEDDING_MODEL)").set_defaults(
        func=cmd_embed
    )
    sub.add_parser("check-llm", help="LLM bağlantısını ve desteklenen yöntemleri test et").set_defaults(
        func=cmd_check_llm
    )
    backup = sub.add_parser("backup", help="Veritabanının tutarlı yedeğini al (uygulama çalışırken de güvenli)")
    backup.add_argument("--out", help="Yedek dosyası (varsayılan: data/backups/app-TARIH.db)")
    backup.set_defaults(func=cmd_backup)
    demo = sub.add_parser("demo-data", help="Yerel deneme için örnek sözleşme PDF'leri üret (reportlab gerekir)")
    demo.add_argument("--dir", help="Hedef klasör (varsayılan: LOCAL_CONTRACTS_DIR)")
    demo.set_defaults(func=cmd_demo_data)
    sub.add_parser("hash-password", help="AUTH_MODE=basic için bcrypt şifre özeti üret").set_defaults(
        func=cmd_hash_password
    )
    turkuaz = sub.add_parser("check-turkuaz", help="Turkuaz'a bağlan, ilk kayıtları ve alan adlarını göster")
    turkuaz.add_argument("--out", help="Ham JSON örneklerini bu dosyaya yaz (base64 içerik kısaltılır)")
    turkuaz.set_defaults(func=cmd_check_turkuaz)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
