"""Domain exception classes used across the pipeline.

PipelineError carries `stage` and `status_code` so the HTTP layer can map it
directly to a JSON error response without leaking stack traces or guessing
status codes.
"""
from __future__ import annotations


class PipelineError(Exception):
    """Raised when the pipeline can't proceed for a known, expected reason
    (missing data, conflicting state, bad config).

    Always caught in V1.routes.api_route and returned as a 4xx JSON response.
    Unlike SystemExit, this does NOT terminate the Flask worker.
    """

    def __init__(self, message: str, stage: str = "unknown", status_code: int = 400):
        super().__init__(message)
        self.stage = stage
        self.status_code = status_code
