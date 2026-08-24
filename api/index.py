"""
Vercel Serverless 入口 — 将 Flask app 暴露为 WSGI 应用。
Vercel Python 运行时会自动检测 api/ 目录下的 .py 文件并将其包装为 Serverless Function。
"""
import sys
import os

# 将项目根目录加入 sys.path，使 app.py / reports.py 可被正常导入
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from app import app  # noqa: E402
