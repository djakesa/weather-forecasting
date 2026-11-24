"""ASGI entry point for the local Atlas Solaire server."""
from .adapters.http import create_app

app = create_app()
