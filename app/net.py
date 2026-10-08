"""Outbound network helpers shared by the LLM and Turkuaz clients."""

from __future__ import annotations

import ssl
from pathlib import Path

from app.config import Settings


def build_ssl_context(settings: Settings) -> ssl.SSLContext:
    """System trust store plus the optional corporate CA.

    Note: httpx/httpx2 honour `SSL_CERT_FILE` by trusting *only* that file, which
    breaks public endpoints when the file holds just the corporate root. We load
    the system defaults first and add the corporate bundle on top.
    """
    context = ssl.create_default_context()
    ca_path = settings.ca_bundle_path
    if ca_path:
        if not Path(ca_path).is_file():
            raise FileNotFoundError(f"CA sertifika dosyası bulunamadı: {ca_path}")
        context.load_verify_locations(cafile=ca_path)
    return context
