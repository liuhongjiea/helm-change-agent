#!/usr/bin/env python3
"""Entry point: python run.py"""
import os
from pathlib import Path

# Load .env before importing app modules
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

import uvicorn

if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("server.app:app", host=host, port=port, reload=True, reload_dirs=["server"])
