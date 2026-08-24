"""API 边界中间件。"""
from __future__ import annotations

from starlette.responses import PlainTextResponse


class RequestBodyLimitMiddleware:
    """在 Starlette 解析请求体前按接收字节数拒绝超限请求。"""

    def __init__(self, app, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                if int(content_length) > self.max_bytes:
                    await self._reject(scope, receive, send)
                    return
            except ValueError:
                await self._reject(scope, receive, send)
                return

        received = 0

        class BodyTooLarge(Exception):
            pass

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise BodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except BodyTooLarge:
            await self._reject(scope, receive, send)

    @staticmethod
    async def _reject(scope, receive, send) -> None:
        response = PlainTextResponse("Request body too large", status_code=413)
        await response(scope, receive, send)
