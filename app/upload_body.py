"""Bound streamed bodies before retaining or decoding them."""
import asyncio

from fastapi import HTTPException, Request
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse

from app.product_errors import chat_http_error, is_chat_request

_JSON_PATHS = {"/api/v1/chat", "/api/v1/approvals", "/api/v1/query"}


async def bounded_body(
    request: Request, limit: int, *, size_error: str = "文件超过 10 MiB",
) -> bytes:
    length = request.headers.get("content-length")
    if length is not None:
        if not length.isascii() or not length.isdecimal() or len(length) > 20:
            raise HTTPException(400, "请求长度无效")
        if int(length) > limit:
            raise HTTPException(413, size_error)

    data = bytearray()
    try:
        async with asyncio.timeout(20):
            async for chunk in request.stream():
                if len(data) + len(chunk) > limit:
                    raise HTTPException(413, size_error)
                data.extend(chunk)
    except TimeoutError as exc:
        raise HTTPException(408, "读取请求超时") from exc
    except ClientDisconnect as exc:
        raise HTTPException(400, "请求上传中断") from exc
    return bytes(data)


class BoundedJSON:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] not in _JSON_PATHS:
            return await self.app(scope, receive, send)
        request = Request(scope, receive)
        try:
            data = await bounded_body(request, 16 * 1024, size_error="JSON 请求超过 16 KiB")
        except HTTPException as exc:
            if is_chat_request(request):
                response = await chat_http_error(request, exc.status_code, exc.detail)
            else:
                response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            return await response(scope, receive, send)

        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": data, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
