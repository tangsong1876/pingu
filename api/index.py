# Vercel Serverless Function 入口
# 通过 vercel.json 的 routes，所有请求都转发到这里由 Flask 处理。
import os
import sys

# 把项目根目录加入模块搜索路径，确保能从 api/ 子目录导入根目录的 app.py
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import app

# Vercel 的 Python 运行时需要暴露一个 WSGI 可调用对象（app 或 application）。
# Flask 实例本身就是 WSGI 应用，直接复用即可。
application = app
