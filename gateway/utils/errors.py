"""
One shape for the errors the gateway's own code answers with. (A request that
fails FastAPI's validation on an admin endpoint still gets FastAPI's body.)

The body always goes through the JSON encoder. Building it by hand with an
f-string put request paths into the JSON unescaped, so a quote in a URL
produced an invalid document.
"""

from fastapi.responses import JSONResponse
from models.schemas import ErrorResponse


def error_response(
    status_code: int,
    error: str,
    *,
    retry_after: int | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse(error=error, retry_after=retry_after)
    return JSONResponse(status_code=status_code, content=body.model_dump(exclude_none=True), headers=headers)
