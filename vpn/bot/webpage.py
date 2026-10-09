"""Account page shown when a subscription link is opened in a web browser."""
import html
import time

import segno

import db
import fmt
import links
from config import cfg

_CLIENTS = ("v2ray", "xray", "clash", "sing-box", "singbox", "hiddify", "streisand", "shadowrocket",
            "v2box", "nekobox", "nekoray", "foxray", "quantumult", "surge", "stash", "karing", "happ")


def is_browser(request) -> bool:
    ua = request.headers.get("User-Agent", "").lower()
    return ("mozilla" in ua and not any(c in ua for c in _CLIENTS)
            and "text/html" in request.headers.get("Accept", ""))


def render(u) -> str:
    sub = links.sub_url(u)
    qr = segno.make(sub, error="m").svg_inline(scale=5, dark="#111827", light="#ffffff", border=2)
    used, total = u.used, u.traffic_limit
    pct = min(100, round(used / total * 100)) if total else 0
    if getattr(u, "channel_blocked", 0):
        state, color = "نیازمند عضویت در کانال", "#d97706"
    elif not u.enabled:
        state, color = "غیرفعال", "#dc2626"
    elif (total and pct >= 90) or (u.expire_at and u.expire_at - time.time() < 3 * db.DAY):
        state, color = "رو به اتمام", "#d97706"
    else:
        state, color = "فعال", "#16a34a"
    bot = db.get_setting("bot_username")
    renew = (f'<a class="btn" href="https://t.me/{html.escape(bot)}">🔄 تمدید / خرید از ربات</a>' if bot else "")
    config_rows = "".join(
        f'<div class="cfg"><code>{html.escape(link)}</code>'
        f'<button onclick="cp(this.previousElementSibling.textContent,this)">کپی</button></div>'
        for link in links.all_links(u))
    remain = fmt.size(max(total - used, 0)) if total else "نامحدود"
    bar = (f'<div class="bar"><span style="width:{pct}%;background:{color}"></span></div>'
           f'<div class="muted">{pct}٪ مصرف شده</div>') if total else ""
    return f"""<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(cfg.brand)} — {html.escape(u.name)}</title>
<style>
:root{{--bg:#f3f4f6;--card:#fff;--fg:#111827;--muted:#6b7280;--line:#e5e7eb;--accent:#2563eb}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0b1220;--card:#111827;--fg:#f9fafb;--muted:#9ca3af;--line:#1f2937;--accent:#60a5fa}}}}
*{{box-sizing:border-box}}html,body{{overflow-x:hidden}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 Tahoma,system-ui,sans-serif}}
.wrap{{max-width:480px;margin:0 auto;padding:16px}}.card{{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px;margin-bottom:14px}}
h1{{font-size:20px;margin:0 0 4px}}.pill{{display:inline-block;padding:2px 10px;border-radius:99px;color:#fff;font-size:13px;background:{color}}}
.grid{{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:10px;margin-top:12px}}.stat{{background:var(--bg);border-radius:12px;padding:10px}}
.stat b{{display:block;font-size:17px}}.muted{{color:var(--muted);font-size:13px}}
.bar{{height:10px;background:var(--line);border-radius:99px;overflow:hidden;margin-top:12px}}.bar span{{display:block;height:100%}}
.qr{{text-align:center}}.qr svg{{display:block;margin:8px auto;max-width:220px;width:100%;height:auto;border-radius:12px}}
.cfg{{display:flex;gap:8px;align-items:center;margin-top:8px}}.cfg code{{flex:1;min-width:0;direction:ltr;font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;background:var(--bg);padding:8px;border-radius:8px}}
button,.btn{{border:0;background:var(--accent);color:#fff;border-radius:10px;padding:8px 14px;font:inherit;cursor:pointer;text-decoration:none;display:inline-block}}
.btn{{display:block;text-align:center;padding:12px}}
</style></head><body><div class="wrap">
<div class="card"><h1>{html.escape(cfg.brand)}</h1><span class="pill">{state}</span> <span class="muted">{html.escape(u.name)}</span>
<div class="grid"><div class="stat"><span class="muted">باقی‌مانده</span><b>{remain}</b></div>
<div class="stat"><span class="muted">اعتبار</span><b>{fmt.remaining_days(u)}</b></div>
<div class="stat"><span class="muted">مصرف</span><b>{fmt.size(used)}</b></div>
<div class="stat"><span class="muted">کل حجم</span><b>{fmt.size(total) if total else "نامحدود"}</b></div></div>{bar}</div>
<div class="card qr"><div class="muted">QR سابسکریپشن را در اپ اسکن کنید</div>{qr}
<div class="cfg"><code>{html.escape(sub)}</code><button onclick="cp(this.previousElementSibling.textContent,this)">کپی</button></div></div>
<div class="card"><b>کانفیگ‌ها</b>{config_rows}</div>
{renew}
</div><script>
function cp(t,b){{(navigator.clipboard?navigator.clipboard.writeText(t):Promise.reject()).then(()=>ok(b)).catch(()=>{{const a=document.createElement('textarea');a.value=t;document.body.appendChild(a);a.select();document.execCommand('copy');a.remove();ok(b)}})}}
function ok(b){{const o=b.textContent;b.textContent='✓';setTimeout(()=>b.textContent=o,1200)}}
</script></body></html>"""
