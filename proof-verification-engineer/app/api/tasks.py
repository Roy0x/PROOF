"""Task API boundary.

The controlled Demo Mode routes remain in ``app.main`` until a later behavior-preserving
move can include their in-memory target application state.
"""
from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["tasks"])
