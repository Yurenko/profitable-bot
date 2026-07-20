"""Vercel ASGI entrypoint.

Exposes the existing FastAPI dashboard app for Vercel Python runtime.
"""
from dashboard.server import app

