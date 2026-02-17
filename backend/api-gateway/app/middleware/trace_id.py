"""
TraceID Middleware - 生成和透传TraceID
"""

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from shared.utils.trace_id import generate_trace_id

class TraceIDMiddleware(BaseHTTPMiddleware):
    """TraceID中间件"""
    
    async def dispatch(self, request: Request, call_next):
        # 从请求头获取或生成新的TraceID
        trace_id = request.headers.get("X-Trace-ID")
        if not trace_id:
            trace_id = generate_trace_id()
        
        # 将TraceID注入到request state
        request.state.trace_id = trace_id
        
        # 调用下一个中间件/路由
        response = await call_next(request)
        
        # 在响应头中返回TraceID
        response.headers["X-Trace-ID"] = trace_id
        
        return response
