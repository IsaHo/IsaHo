"""TC VPN panel bridge, phase one: pinned TLS, monitor-only API, no account writes."""
import asyncio
import hashlib
import hmac
import http.client
import json
from pathlib import Path
import re
import ssl
from urllib.parse import urlsplit

from aiogram import F, Router
from aiogram.types import CallbackQuery

router = Router()
CONFIG_PATH = '/etc/isaho-vpn/tc-panel.json'


class PanelUnavailable(Exception):
    """Intentionally contains no remote URL, credential or API response."""


def load_config(path=CONFIG_PATH):
    try:
        p = Path(path)
        if p.stat().st_mode & 0o077:
            raise ValueError('private configuration required')
        value = json.loads(p.read_text())
        url = urlsplit(value['url'])
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('invalid HTTPS endpoint')
        pin = value['sha256'].lower()
        if not re.fullmatch('[0-9a-f]{64}', pin) or not value['token']:
            raise ValueError('invalid credential')
        return {**value, 'sha256': pin}
    except (OSError, ValueError, KeyError, TypeError):
        raise PanelUnavailable('panel configuration unavailable') from None


def read_status(config):
    """Verify the SSH-enrolled leaf pin BEFORE transmitting the bearer token.

    Do not follow redirects, use environment proxies, reset traffic counters, or
    expose the full server status (it can contain operational information).
    """
    connection = None
    try:
        url = urlsplit(config['url'])
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # Exact out-of-band leaf pin below.
        connection = http.client.HTTPSConnection(url.hostname, url.port or 443, timeout=8, context=context)
        connection.connect()
        digest = hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
        if not hmac.compare_digest(digest, config['sha256']):
            raise PanelUnavailable('panel certificate changed')
        path = url.path.rstrip('/') + '/panel/api/server/status'
        connection.request('GET', path, headers={'Authorization': 'Bearer ' + config['token'], 'Accept': 'application/json'})
        response = connection.getresponse()
        if response.status != 200:
            raise PanelUnavailable('panel status unavailable')
        body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise PanelUnavailable('panel response too large')
        result = json.loads(body)
        if not isinstance(result, dict) or result.get('success') is not True or not isinstance(result.get('obj'), dict):
            raise PanelUnavailable('invalid panel status')
        obj = result['obj']
        xray = obj.get('xray') or {}
        if not isinstance(xray, dict):
            raise PanelUnavailable('invalid panel status')
        state = xray.get('state', 'unknown')
        return {'xray_running': state == 'running', 'uptime': max(0, int(obj.get('uptime') or 0)),
                'monitor_only': True}
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
        raise PanelUnavailable('panel status unavailable') from None
    finally:
        if connection:
            connection.close()


@router.callback_query(F.data == 'tcp:status')
async def status_callback(cb: CallbackQuery):
    import db
    import handlers as h
    if cb.from_user.id not in db.admin_ids():
        await cb.answer('این بخش مخصوص مدیر است.', show_alert=True)
        return
    # Acknowledge first; never hold Telegram's spinner through a network timeout.
    await cb.answer()
    try:
        config = load_config()
        status = await asyncio.to_thread(read_status, config)
        state = '🟢 فعال' if status['xray_running'] else '🟡 نیازمند بررسی'
        text = ('🖥 <b>TC VPN › پنل وب</b>\n\n'
                '🔐 ارتباط مدیریتی با گواهی پین‌شده\n'
                f'⚡ موتور اتصال: {state}\n'
                f'⏱ آپتایم سرور: {status["uptime"] // 3600} ساعت\n\n'
                '👁 مرحلهٔ اول: پایش فقط‌خواندنی\n'
                'حساب‌های فعلی، حجم و سابسکریپشن تغییر نمی‌کنند.\n'
                'تمام امکانات پنل وب حفظ شده‌اند.')
    except PanelUnavailable:
        text = ('🖥 <b>TC VPN › پنل وب</b>\n\n'
                '🟡 ارتباط مدیریتی فعلاً قابل تأیید نیست.\n'
                'این خطا به‌تنهایی به معنی قطع VPN نیست؛ حساب‌ها تغییری نکردند.')
    await cb.message.answer(text, reply_markup=h.ikb([
        [('🔄 بررسی دوباره', 'tcp:status')], [('↩️ مدیریت', 'nav:manage')],
    ]))
