"""
Vercel Serverless 入口 — 将 Flask app 暴露为 Vercel Python Function handler。

Vercel 的 Python Runtime 默认支持 WSGI app，但 vercel.json 的 rewrite 规则
`/(.*) -> /api/index` 会让 Flask 收到的 PATH_INFO 被改写为空字符串（或 `/api/index`），
导致所有 `@app.route` 全部 404。

因此这里定义 `handler(request, **kwargs)`，手动从 Vercel request 对象还原原始
PATH_INFO，再调用 Flask WSGI app。
"""
import sys
import os
import io

# 将项目根目录加入 sys.path，使 app.py / reports.py 可被正常导入
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from app import app as flask_app  # noqa: E402


def handler(request, **kwargs):
    """Vercel Python Function 入口。"""
    # body
    body = request.body
    if body is None:
        body = b""
    elif isinstance(body, str):
        body = body.encode("utf-8")
    elif not isinstance(body, bytes):
        body = bytes(body)
    input_stream = io.BytesIO(body)

    # scheme / host / path
    url = request.url or "https://localhost/"
    scheme = url.split("://")[0] if "://" in url else "https"
    path = request.path or "/"
    query = request.query_string
    if isinstance(query, bytes):
        query = query.decode("utf-8")
    elif query is None:
        query = ""
    else:
        query = str(query)

    host = request.headers.get("host") or request.headers.get("Host") or "localhost"

    environ = {
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": scheme,
        "wsgi.input": input_stream,
        "wsgi.errors": sys.stderr,
        "wsgi.multithread": False,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
        "REQUEST_METHOD": request.method or "GET",
        "SCRIPT_NAME": "",
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "SERVER_PROTOCOL": "HTTP/1.1",
        "HTTP_HOST": host,
    }

    if body:
        environ["CONTENT_LENGTH"] = str(len(body))
    content_type = request.headers.get("content-type") or request.headers.get("Content-Type")
    if content_type:
        environ["CONTENT_TYPE"] = content_type

    # 透传其他 headers
    for key, value in request.headers.items():
        if not value:
            continue
        header_key = key.upper().replace("-", "_")
        if header_key in ("CONTENT_TYPE", "CONTENT_LENGTH"):
            environ[header_key] = str(value)
        else:
            environ[f"HTTP_{header_key}"] = str(value)

    response_data = {}

    def start_response(status, response_headers, exc_info=None):
        response_data["status"] = status
        response_data["headers"] = response_headers

    body_iter = flask_app(environ, start_response)
    output = b""
    for chunk in body_iter:
        if chunk:
            output += chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")

    headers = {}
    for h_key, h_value in response_data.get("headers", []):
        headers[h_key] = h_value

    return {
        "statusCode": int(response_data["status"].split(" ")[0]),
        "headers": headers,
        "body": output.decode("utf-8"),
    }
