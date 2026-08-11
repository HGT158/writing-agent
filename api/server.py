"""Uvicorn entry point; importing ``api.main`` remains side-effect free."""
from .main import create_app

app = create_app()
