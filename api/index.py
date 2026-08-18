# Vercel Serverless Function 入口
# 通过 vercel.json 的 routes，所有请求都转发到这里由 Flask 处理。
from app import app

# Vercel Python 运行时需要暴露一个 WSGI handler
def handler(environ, start_response):
    return app(environ, start_response)
