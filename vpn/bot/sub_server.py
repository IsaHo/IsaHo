"""HTTPS subscription endpoint (served through Cloudflare with SSL mode "Full")."""
import base64
import logging
import ssl

from aiohttp import web

import db
import links
from config import cfg

log = logging.getLogger(__name__)


async def _sub(request: web.Request) -> web.Response:
    user = db.get_by_token(request.match_info["token"])
    if not user:
        raise web.HTTPNotFound()
    title = base64.b64encode(f"{cfg.brand} - {user.name}".encode()).decode()
    return web.Response(
        text=links.sub_body(user),
        headers={
            "Content-Type": "text/plain; charset=utf-8",
            "Subscription-Userinfo": links.sub_userinfo(user),
            "Profile-Update-Interval": "6",
            "Profile-Title": f"base64:{title}",
            "Content-Disposition": f'attachment; filename="{cfg.brand}"',
            "Cache-Control": "no-store",
        },
    )


async def start() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/sub/{token}", _sub)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cfg.cert_file, cfg.key_file)
    await web.TCPSite(runner, "0.0.0.0", cfg.sub_port, ssl_context=ctx).start()
    log.info("subscription server on :%s", cfg.sub_port)
    return runner
