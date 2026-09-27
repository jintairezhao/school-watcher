"""Private JSON execution API and revocable noVNC binary WebSocket bridge."""
import asyncio
import os
import secrets
import time

from aiohttp import web, WSMsgType

from .runtime import BrowserRuntime, RuntimeFault


def create_service(runtime=None, token=None, public_origin=None):
    token = token or os.environ.get('WATCHER_BROWSER_TOKEN', '')
    if len(token) < 32:
        raise ValueError('WATCHER_BROWSER_TOKEN must contain at least 32 characters')
    public_origin = (public_origin or os.environ.get('WATCHER_PUBLIC_ORIGIN', '')).rstrip('/')
    runtime = runtime or BrowserRuntime(
        concurrency=int(os.environ.get('WATCHER_BROWSER_CONCURRENCY', '1')),
        max_sessions=int(os.environ.get('WATCHER_BROWSER_MAX_SESSIONS', '8')),
        max_executions=int(os.environ.get('WATCHER_BROWSER_MAX_EXECUTIONS', '128')))

    @web.middleware
    async def protect(request, handler):
        try:
            is_socket = request.path.endswith('/websocket') and request.method == 'GET'
            if is_socket:
                # Browser cookies are authorized by the reverse proxy's Flask auth_request;
                # a one-use secret ticket independently authenticates this private service.
                if not public_origin or request.headers.get('Origin', '').rstrip('/') != public_origin:
                    raise RuntimeFault('origin_denied', '验证连接来源不正确', 403)
            elif not secrets.compare_digest(request.headers.get('X-Watcher-Token', ''), token):
                raise RuntimeFault('unauthorized', '未授权的浏览器请求', 401)
            response = await handler(request)
            response.headers['Cache-Control'] = 'no-store'
            return response
        except RuntimeFault as exc:
            return web.json_response({'error': exc.public()}, status=exc.status,
                headers={'Cache-Control': 'no-store'})
        except (ValueError, TypeError, KeyError):
            return web.json_response({'error': {'code': 'invalid_request', 'message': '请求参数不正确', 'retryable': False}}, status=400)

    app = web.Application(middlewares=[protect], client_max_size=65536)

    async def body(request):
        value = await request.json()
        if not isinstance(value, dict):
            raise ValueError('object required')
        return value

    async def health(request):
        return web.json_response({'status': 'ok', 'runtime_id': runtime.boot_id,
            'sessions': len(runtime.sessions), 'executions': len(runtime.executions),
            'active_operations': len(runtime.operation_deadlines),
            'manual_active': sum(v['state'] == 'active' for v in runtime.verifications.values())})

    async def submit(request):
        return web.json_response(await runtime.submit(await body(request)), status=202)

    async def poll(request):
        return web.json_response(runtime.get_execution(request.match_info['ident']))

    async def manual_open(request):
        return web.json_response(await runtime.open_manual(await body(request)))

    async def manual_status(request):
        return web.json_response(runtime.manual_status(request.match_info['ident'], request.headers.get('X-Session-Generation', '')))

    async def manual_action(request):
        data = await body(request)
        ident, action = request.match_info['ident'], request.match_info['action']
        generation = data.get('generation', '')
        if action == 'verify':
            result = await runtime.verify_manual(ident, generation)
        elif action == 'cancel':
            result = await runtime.end_manual(ident, generation)
        elif action == 'ticket':
            result = runtime.issue_ticket(ident, generation)
        else:
            raise RuntimeFault('invalid_action', '操作不存在', 404)
        return web.json_response(result)

    async def websocket(request):
        verification = runtime.consume_ticket(request.match_info['ident'], request.query.get('ticket', ''))
        port = verification['session'].display.port
        if not port:
            raise RuntimeFault('display_unavailable', '请在本机浏览器窗口完成验证', 409)
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        ws = web.WebSocketResponse(protocols=('binary',), max_msg_size=1024 * 1024, heartbeat=20)
        await ws.prepare(request)
        verification['sockets'].add(ws)

        async def receive_vnc():
            while not ws.closed:
                data = await reader.read(65536)
                if not data:
                    break
                await ws.send_bytes(data)
            await ws.close()

        pump = asyncio.create_task(receive_vnc())
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    writer.write(msg.data)
                    await writer.drain()
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            verification['sockets'].discard(ws)
            writer.close()
            await writer.wait_closed()
            pump.cancel()  # Socket pump only; never cancels a Playwright operation.
            await asyncio.gather(pump, return_exceptions=True)
            await ws.close()
        return ws

    app.router.add_get('/health', health)
    app.router.add_post('/v1/fetch', submit)
    app.router.add_get('/v1/fetch/{ident}', poll)
    app.router.add_post('/v1/manual', manual_open)
    app.router.add_get('/v1/manual/{ident}', manual_status)
    app.router.add_post('/v1/manual/{ident}/{action}', manual_action)
    app.router.add_get('/v1/manual/{ident}/websocket', websocket)

    async def lifecycle(app):
        async def maintenance():
            while True:
                await asyncio.sleep(2)
                if any(deadline < time.monotonic() for deadline in runtime.operation_deadlines.values()):
                    # A native call failed to honor its own deadline. Supervisor restarts
                    # this isolated process; durable task leases recover in the worker.
                    os._exit(75)
                await runtime.sweep()
        sweeper = asyncio.create_task(maintenance())
        yield
        sweeper.cancel()
        await asyncio.gather(sweeper, return_exceptions=True)
        await runtime.close()
    app.cleanup_ctx.append(lifecycle)
    return app


def main():
    # Container bind-all is opt-in; deploy on an internal network without a published port.
    web.run_app(create_service(), host=os.environ.get('WATCHER_BROWSER_HOST', '127.0.0.1'),
        port=int(os.environ.get('WATCHER_BROWSER_PORT', '8765')), access_log=None,
        shutdown_timeout=100)
