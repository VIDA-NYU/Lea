"""Compatibility endpoints for the retired human correspondence review flow.

Lea Status carries correspondence findings. Approval events remain historical
records and no longer change a formalization's checked outcome.
"""
from __future__ import annotations

from fastapi import HTTPException


def retired():
    raise HTTPException(status_code=410, detail={
        'code': 'formalization_review_retired',
        'message': 'Lea Status and its description replace result review. No approval is required.',
    })


def context(fid, source_context_ref=None):
    retired()


def decide(fid, request):
    retired()
