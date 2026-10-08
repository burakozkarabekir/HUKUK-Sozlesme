"""Command-line tools.

Usage:
    python cli.py migrate          # apply DB migrations
    python cli.py stats            # row counts and schema version
    python cli.py sync [--full]    # fetch + index contracts (incremental)
    python cli.py reindex          # re-chunk stored PDFs, rebuild FTS
    python cli.py embed            # create missing embeddings (vector search)
    python cli.py check-llm        # LLM connectivity + supported features
    python cli.py check-turkuaz    # (phase 6)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections.abc import Awaitable, Callable

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
    sub.add_parser("check-turkuaz", help="Turkuaz endpoint'ini test et").set_defaults(func=_not_yet(6))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
