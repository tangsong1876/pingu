# Vercel Serverless Function 入口
# 通过 vercel.json 的 rewrite，所有请求（页面 + /api）都转发到这里由 Flask 处理。
from app import app
