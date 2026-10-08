"""HTTPS subscription endpoint (served through Cloudflare with SSL mode "Full")."""
import base64
import logging
import ssl

from aiohttp import web

import db
import links
import relays
from config import cfg

log = logging.getLogger(__name__)


async def _sub(request: web.Request) -> web.Response:
    user = db.get_by_token(request.match_info["token"])
    if not user:
        raise web.HTTPNotFound()
    import webpage
    if webpage.is_browser(request):
        return web.Response(text=webpage.render(user), content_type="text/html", charset="utf-8",
                            headers={"Cache-Control": "no-store"})
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


async def _relay_report(request: web.Request) -> web.Response:
    if not relays.is_local_request(request):
        raise web.HTTPNotFound()
    try:
        data = await request.json()
    except ValueError:
        raise web.HTTPBadRequest()
    if not isinstance(data, dict):
        raise web.HTTPBadRequest()
    reply = relays.record(data)
    import health
    probe = health.relay_job(data)
    if probe:
        reply["probe"] = probe
    return web.json_response(reply)


async def _pay_sms(request: web.Request) -> web.Response:
    import shop
    import smspay
    if not request.query.get("key") or request.query.get("key") != db.get_setting("sms_key"):
        raise web.HTTPNotFound()
    if request.content_type == "application/json":
        text = (await request.json()).get("text", "")
    elif request.content_type in ("application/x-www-form-urlencoded", "multipart/form-data"):
        text = (await request.post()).get("text", "")
    else:
        text = await request.text()
    result = await shop.handle_sms(str(text)[:2000]) if smspay.enabled() else "disabled"
    return web.Response(text=result)


async def _node_config(request: web.Request) -> web.Response:
    import nodes
    node = nodes.by_key(request.query.get("key", ""))
    if not node:
        raise web.HTTPNotFound()
    return web.json_response(nodes.config_for(node))


async def _node_stats(request: web.Request) -> web.Response:
    import nodes
    node = nodes.by_key(request.query.get("key", ""))
    if not node:
        raise web.HTTPNotFound()
    try:
        data = await request.json()
    except ValueError:
        raise web.HTTPBadRequest()
    nodes.record_stats(node, data.get("stats") or {}, data.get("info") or {})
    return web.json_response({"ok": True})


async def _node_backup(request: web.Request) -> web.Response:
    import nodes
    if not nodes.by_key(request.query.get("key", "")):
        raise web.HTTPNotFound()
    return web.json_response(nodes.standby_bundle())


async def start() -> web.AppRunner:
    app = web.Application(client_max_size=64 * 1024)
    app.router.add_get("/sub/{token}", _sub)
    app.router.add_post("/relay/report", _relay_report)
    app.router.add_post("/pay/sms", _pay_sms)
    app.router.add_get("/node/config", _node_config)
    app.router.add_post("/node/stats", _node_stats)
    app.router.add_get("/node/backup", _node_backup)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cfg.cert_file, cfg.key_file)
    await web.TCPSite(runner, "0.0.0.0", cfg.sub_port, ssl_context=ctx).start()
    # plain HTTP for the SSH tunnels from Iranian relays (never exposed publicly)
    await web.TCPSite(runner, "127.0.0.1", cfg.relay_sub_port).start()
    log.info("subscription server on :%s (relay :%s)", cfg.sub_port, cfg.relay_sub_port)
    return runner
