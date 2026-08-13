from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor


# PBKDF2, RSA and large-file E2EE work share one deliberately small pool.
# Keeping this process-wide prevents each Bot from multiplying worker threads.
CRYPTO_EXECUTOR = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="RocketCatCrypto",
)
