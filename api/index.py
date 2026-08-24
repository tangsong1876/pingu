"""
Vercel Serverless 入口。

真正的 Vercel 路径修复包装器已经放在 app.py 末尾：
当环境变量 VERCEL=1 且 PATH_INFO 为 /api/index 时，从 query string
的 path 参数还原出原始路径，再交给 Flask 路由。

这里只需要暴露包装后的 app 即可。
"""
import sys
import os

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from app import app  # noqa: E402,F401
