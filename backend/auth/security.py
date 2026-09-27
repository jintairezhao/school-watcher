"""安全响应头"""
from flask import Flask, request


def register_security_headers(app: Flask):
    """为所有响应添加安全相关的 HTTP 头"""

    @app.after_request
    def set_security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        if app.config.get('DESKTOP_MODE'):
            response.headers['Referrer-Policy'] = 'no-referrer'
            if not request.path.startswith('/static/'):
                response.headers['Cache-Control'] = 'no-store'
        response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https: http:; "
            "connect-src 'self' https://api.deepseek.com; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self';"
        )
        return response
