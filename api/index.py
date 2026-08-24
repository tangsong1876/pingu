"""
Vercel Serverless 入口 — 将 Flask app 暴露为 WSGI 应用。

Vercel 的 rewrite 规则 `/(.*) -> /api/index?path=/$1` 会把原始请求路径
通过 query string 传入，而函数收到的 PATH_INFO 是 /api/index。
这里用 WSGI 包装器把 PATH_INFO 从 query 中的 path 参数还原为真实路径，
确保 Flask 路由正常匹配。
"""
import sys
import os
from urllib.parse import parse_qs

# 将项目根目录加入 sys.path，使 app.py / reports.py 可被正常导入
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def app(environ, start_response):
    """WSGI 包装器：还原 Vercel rewrite 丢失的原始 PATH_INFO。"""
    # 延迟导入真正的 Flask app，避免顶层暴露其它 WSGI callable 被 Vercel 误选
    from app import app as flask_app  # noqa: E402

    path = environ.get("PATH_INFO", "")
    if path == "/api/index":
        raw_qs = environ.get("QUERY_STRING", "")
        params = parse_qs(raw_qs)
        real_path = params.get("path", ["/"])
        environ["PATH_INFO"] = real_path[0] if isinstance(real_path, list) else real_path
        # 移除已消费的 path 参数，保留其它查询参数
        rest = {k: v for k, v in params.items() if k != "path"}
        environ["QUERY_STRING"] = "&".join(
            f"{k}={v[0]}" for k, v in rest.items()
        )
    return flask_app(environ, start_response)
