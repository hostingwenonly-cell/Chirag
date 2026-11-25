"""
Cashfree checker API — FastAPI endpoint (fixed ₹230, debit-only).

Usage:
    GET /cashfree?cc=4001679034804536|01|2029|629&proxy=user:pass@host:port&key=ladle

Response:
    {
      "cc": "...",
      "response_code": "APPROVED" | "DECLINED" | "PROXY_ERROR" | "ERROR",
      "response": "...",
      "real_response": "...",
      "amount": "230.00",
      "currency": "INR",
      "gateway": "Cashfree"
    }

Vercel deploy:
    - file name: api.py (this file)
    - pyproject.toml must have: [tool.vercel] entrypoint = "api:app"
    - or rename this file to app.py / main.py / index.py
"""
from __future__ import annotations

import os
import secrets
import logging
from typing import Optional

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# ── cashfree engine ──
try:
    from cashfree import run_cashfree_check, CashfreeResult
except ImportError as e:
    # Vercel build pe cashfree.py missing ho toh clear error
    raise ImportError(
        "cashfree.py not found in project root. "
        "Ensure cashfree.py is next to api.py and listed in requirements.txt."
    ) from e

# ── logging ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logger = logging.getLogger("cashfree-api")

# ── config ──
API_KEY = os.environ.get("CASHFREE_API_KEY", "ladle")
DEFAULT_AMOUNT = "230.00"
DEFAULT_CURRENCY = "INR"
GATEWAY_NAME = "Cashfree"

# ── FastAPI app ──
app = FastAPI(
    title="Cashfree Checker API",
    version="1.1.0",
    description="Fixed ₹230 debit-only Cashfree card checker.",
)

# CORS — sab origins allow (public checker)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HELPERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _sanitize(res: CashfreeResult) -> dict:
    """CashfreeResult → JSON-safe dict."""
    return {
        "cc": getattr(res, "cc", ""),
        "response_code": getattr(res, "response_code", "ERROR"),
        "response": getattr(res, "response", ""),
        "real_response": getattr(res, "real_response", ""),
        "amount": getattr(res, "amount", DEFAULT_AMOUNT),
        "currency": getattr(res, "currency", DEFAULT_CURRENCY),
        "gateway": getattr(res, "gateway", GATEWAY_NAME),
    }


def _error_payload(cc: str, code: str, message: str = "ERROR") -> dict:
    """Error response ka standard shape."""
    return {
        "cc": cc,
        "response_code": code,
        "response": message,
        "real_response": message,
        "amount": DEFAULT_AMOUNT,
        "currency": DEFAULT_CURRENCY,
        "gateway": GATEWAY_NAME,
    }


def _parse_proxy(raw: str) -> str:
    """
    Proxy string ko http://user:pass@host:port mein normalize karo.

    Accepts:
        user:pass@host:port
        host:port:user:pass
        http://user:pass@host:port
        socks5://user:pass@host:port
        host:port           (no auth)
    """
    p = (raw or "").strip()
    if not p:
        return ""

    # already has scheme
    if "://" in p:
        return p

    # user:pass@host:port — has @, just prefix scheme
    if "@" in p:
        return "http://" + p

    # host:port:user:pass (4 parts, no @)
    parts = p.split(":")
    if len(parts) == 4:
        host, port, user, pw = parts
        return f"http://{user}:{pw}@{host}:{port}"

    # host:port (no auth)
    if len(parts) == 2:
        return "http://" + p

    # unknown — try as-is with scheme
    return "http://" + p


def _classify_error(err: Exception) -> str:
    """Exception ko PROXY_ERROR / ERROR mein classify karo."""
    msg = (str(err) or "").lower()
    proxy_keywords = (
        "proxy", "curl", "timed out", "timeout", "connect",
        "resolve", "tunnel", "refused", "ssl", "certificate",
    )
    if any(k in msg for k in proxy_keywords):
        return "PROXY_ERROR"
    return "ERROR"


def _check_key(provided: str) -> bool:
    """Timing-safe API key check."""
    try:
        return secrets.compare_digest(provided or "", API_KEY)
    except Exception:
        return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUTES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@app.get("/cashfree")
async def cashfree(
    cc: str = Query(default="", description="card|mm|yyyy|cvv"),
    proxy: str = Query(default="", description="proxy string"),
    key: str = Query(default="", description="API key"),
):
    """Card check endpoint."""
    # auth
    if not _check_key(key):
        return JSONResponse(
            {"error": "invalid or missing API key"},
            status_code=401,
        )

    # validate card input
    if not cc or cc.count("|") != 3:
        return JSONResponse(
            {"error": "missing or invalid cc (expected number|mm|yyyy|cvv)"},
            status_code=400,
        )

    proxy_url = _parse_proxy(proxy)
    logger.info(f"check start cc={cc[:6]}... proxy={'yes' if proxy_url else 'direct'}")

    try:
        res = await run_cashfree_check(cc, proxy_url)
        payload = _sanitize(res)
        logger.info(
            f"check done cc={cc[:6]}... code={payload['response_code']} "
            f"msg={payload['response'][:60]}"
        )
        return JSONResponse(payload)
    except Exception as e:
        code = _classify_error(e)
        logger.error(f"check failed cc={cc[:6]}... code={code} err={str(e)[:200]}")
        return JSONResponse(
            _error_payload(cc, code, "ERROR"),
            status_code=200,   # 200 because checker callers expect JSON, not 5xx
        )


@app.get("/health")
async def health():
    """Vercel deploy healthcheck."""
    return {
        "ok": True,
        "api": "cashfree",
        "version": "1.1.0",
        "key_set": bool(API_KEY and API_KEY != "ladle"),
    }


@app.get("/")
async def root():
    """Root — API info."""
    return {
        "ok": True,
        "api": "cashfree",
        "version": "1.1.0",
        "endpoints": ["/cashfree", "/health", "/docs"],
        "usage": "/cashfree?cc=number|mm|yyyy|cvv&proxy=...&key=...",
    }


@app.get("/ping")
async def ping(request: Request):
    """Quick ping — client IP bhi return karta hai (debug ke liye)."""
    client = request.client.host if request.client else "?"
    return {"pong": True, "client_ip": client}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LOCAL DEV ENTRYPOINT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
if __name__ == "__main__":
    import uvicorn
    import argparse

    ap = argparse.ArgumentParser(description="Cashfree checker FastAPI server")
    ap.add_argument("--port", type=int, default=9300)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--reload", action="store_true", help="auto-reload on change")
    args = ap.parse_args()

    print(f"\n  Cashfree API: http://{args.host}:{args.port}")
    print(f"  Health:       http://{args.host}:{args.port}/health")
    print(f"  Docs:         http://{args.host}:{args.port}/docs")
    print(f"  Key set:      {'yes' if API_KEY != 'ladle' else 'default (ladle)'}\n")

    uvicorn.run(
        "api:app" if not args.reload else "api:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=1,
    )