#!/usr/bin/env python3
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import subprocess
import tempfile
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import bot

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("miniapp")

WEBAPP_HTML = Path(__file__).resolve().parent / "webapp" / "index.html"
TICKETS_PATH = bot.DATA_DIR / "tickets.json"
LISTEN_HOST = os.environ.get("MINIAPP_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("MINIAPP_PORT", "8765"))


def verify_tg_init_data(init_data: str, bot_token: str) -> dict | None:
    if not init_data or not bot_token:
        return None
    parsed = urllib.parse.parse_qs(init_data, keep_blank_values=True)
    hash_list = parsed.pop("hash", None)
    if not hash_list:
        return None
    recv_hash = hash_list[0]
    pairs = sorted((k, v[0]) for k, v in parsed.items())
    data_check = "\n".join(f"{k}={v}" for k, v in pairs)
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    calc_hash = hmac.new(secret, data_check.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc_hash, recv_hash):
        return None
    user_raw = parsed.get("user", ["{}"])[0]
    try:
        return json.loads(user_raw)
    except Exception:
        return None


def fmt_ts(ts: int) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def load_tickets() -> dict:
    return bot.load_json(TICKETS_PATH)


def save_tickets(data: dict) -> None:
    bot.save_json(TICKETS_PATH, data)


def get_user_tickets(user_id: int) -> list[dict]:
    all_t = load_tickets()
    mine = [t for t in all_t.values() if int(t.get("user_id", 0)) == user_id]
    mine.sort(key=lambda x: int(x.get("updated_at", 0)), reverse=True)
    for t in mine:
        t["updated_text"] = fmt_ts(int(t.get("updated_at", 0)))
        for m in t.get("messages", []):
            m["time_text"] = fmt_ts(int(m.get("ts", 0)))
    return mine


def notify_admins_ticket(env: dict[str, str], ticket: dict, message_text: str, is_new: bool = True) -> None:
    token = env["BOT_TOKEN"]
    tid = ticket["id"]
    uid = ticket["user_id"]
    uname = ticket.get("username") or "-"
    hdr = "🎫 <b>تیکت پشتیبانی جدید</b>" if is_new else "💬 <b>پاسخ جدید کاربر در تیکت</b>"
    txt = (
        f"{hdr} <code>#{tid}</code>\n\n"
        f"📌 موضوع: <b>{html.escape(ticket['subject'])}</b>\n"
        f"🗂 دسته: {html.escape(ticket.get('category', 'عمومی'))}\n"
        f"👤 کاربر: <code>{uid}</code> (@{html.escape(uname)})\n\n"
        f"📝 پیام:\n{html.escape(message_text)}"
    )
    kb = {
        "inline_keyboard": [
            [
                {"text": f"↩️ پاسخ به #{tid}", "callback_data": f"adm_tk_reply:{tid}"},
                {"text": "🔒 بستن تیکت", "callback_data": f"adm_tk_close:{tid}"},
            ]
        ]
    }
    for adm_id in bot.get_admin_ids(env):
        try:
            bot.send_message(token, adm_id, txt, reply_markup=kb)
        except Exception:
            pass


def fetch_user_subs(env: dict[str, str], user_id: int) -> list[dict]:
    orders = bot.load_json(bot.ORDERS_PATH)
    mine = [o for o in orders.values() if o.get("user_id") == user_id and o.get("status") == "approved" and o.get("panel_username")]
    seen = set()
    res = []
    for o in sorted(mine, key=lambda x: x.get("created_at", 0), reverse=True):
        un = o["panel_username"]
        if un in seen:
            continue
        seen.add(un)
        item = {
            "username": un,
            "sub_url": o.get("sub_url", ""),
            "limit_gb": int(o.get("gb", 0)),
            "used_gb": 0.0,
            "status": "active",
            "expire_text": "-",
        }
        try:
            pu = bot.panel_request(env, f"/api/user/{urllib.parse.quote(un)}")
            used_b = int(pu.get("used_traffic") or 0)
            lim_b = int(pu.get("data_limit") or 0)
            item["used_gb"] = round(used_b / (1024 ** 3), 2)
            if lim_b > 0:
                item["limit_gb"] = round(lim_b / (1024 ** 3), 2)
            item["status"] = pu.get("status") or "active"
            exp = pu.get("expire")
            if exp:
                if isinstance(exp, (int, float)):
                    item["expire_text"] = fmt_ts(int(exp))
                else:
                    item["expire_text"] = str(exp)[:10]
        except Exception:
            pass
        res.append(item)
        if len(res) >= 12:
            break
    return res


class MiniAppHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, code: int, obj: dict) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> dict:
        ln = int(self.headers.get("Content-Length", "0") or 0)
        if ln <= 0 or ln > 262144:
            return {}
        return json.loads(self.rfile.read(ln).decode("utf-8"))

    def _auth_user(self, env: dict[str, str]) -> dict | None:
        init_data = self.headers.get("X-Tg-Init-Data", "")
        u = verify_tg_init_data(init_data, env["BOT_TOKEN"])
        if not u or not u.get("id"):
            self._send_json(401, {"error": "احراز هویت تلگرام نامعتبر است. لطفاً از داخل ربات باز کنید."})
            return None
        return u

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in ("/vpnapp", "/vpnapp/", "/vpnapp/index.html"):
            if not WEBAPP_HTML.exists():
                self.send_error(404)
                return
            data = WEBAPP_HTML.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if path == "/vpnapp/api/qr":
            qs = urllib.parse.parse_qs(parsed.query)
            text = (qs.get("text") or [""])[0][:1000]
            if not text:
                self.send_error(400)
                return
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp_p = Path(tmp.name)
            try:
                subprocess.run(["qrencode", "-o", str(tmp_p), "-s", "8", "-m", "2", "--", text], check=True, timeout=8)
                png = tmp_p.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(png)))
                self.end_headers()
                self.wfile.write(png)
            finally:
                tmp_p.unlink(missing_ok=True)
            return

        env = bot.load_env()
        u = self._auth_user(env)
        if not u:
            return
        uid = int(u["id"])

        if path == "/vpnapp/api/bootstrap":
            cfg = bot.get_settings(env)
            bot_u = env.get("BOT_USERNAME") or "vpnnnVipbot"
            self._send_json(200, {
                "user": {
                    "id": uid,
                    "first_name": u.get("first_name", ""),
                    "username": u.get("username", ""),
                    "balance": bot.get_wallet_balance(uid),
                },
                "settings": {"gb_options": cfg.get("gb_options", bot.DEFAULT_GB_OPTIONS)},
                "subs": fetch_user_subs(env, uid),
                "tickets": get_user_tickets(uid),
                "referral_link": f"https://t.me/{bot_u}?start=ref_{uid}",
            })
            return

        if path == "/vpnapp/api/tickets":
            self._send_json(200, {"tickets": get_user_tickets(uid)})
            return

        self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        env = bot.load_env()
        u = self._auth_user(env)
        if not u:
            return
        uid = int(u["id"])
        token = env["BOT_TOKEN"]
        path = urllib.parse.urlparse(self.path).path
        body = self._read_json()

        if path == "/vpnapp/api/calc":
            gb = max(1, min(5000, int(body.get("gb") or 30)))
            months = max(1, min(24, int(body.get("months") or 1)))
            is_renew = bool(body.get("is_renew"))
            coupon = str(body.get("coupon") or "").strip().upper()
            cpct = 0.0
            if coupon:
                cp = bot.load_json(bot.COUPONS_PATH).get(coupon)
                if not cp or not cp.get("active", True):
                    return self._send_json(400, {"error": "کد تخفیف نامعتبر است"})
                cpct = float(cp.get("percent", 0))
            p = bot.calc_price(gb, months, env=env, is_renew=is_renew, coupon_pct=cpct)
            return self._send_json(200, p)

        if path == "/vpnapp/api/buy_wallet":
            gb = max(1, min(5000, int(body.get("gb") or 30)))
            months = max(1, min(24, int(body.get("months") or 1)))
            target_un = str(body.get("target_username") or "").strip()
            coupon = str(body.get("coupon") or "").strip().upper()
            cpct = 0.0
            if coupon:
                cp = bot.load_json(bot.COUPONS_PATH).get(coupon)
                if cp and cp.get("active", True):
                    cpct = float(cp.get("percent", 0))
            p = bot.calc_price(gb, months, env=env, is_renew=bool(target_un), coupon_pct=cpct)
            cost = int(p["total_toman"])
            if bot.get_wallet_balance(uid) < cost:
                return self._send_json(400, {"error": "موجودی کیف پول کافی نیست"})
            ok_ded, new_bal = bot.adjust_wallet(uid, -cost, f"خرید/تمدید از مینی‌اپ ({gb}GB/{months}M)")
            if not ok_ded:
                return self._send_json(400, {"error": "خطا در کسر موجودی"})
            try:
                if target_un:
                    pu = bot.renew_panel_user(env, target_un, gb, months)
                    sub_url = pu.get("subscription_url") or ""
                    uname = target_un
                else:
                    uname, sub_url = bot.create_panel_user(env, gb, months, uid)
            except Exception as e:
                bot.adjust_wallet(uid, cost, "برگشت وجه خطای ساخت از مینی‌اپ")
                return self._send_json(500, {"error": f"خطا در پنل: {e}"})
            oid = secrets.token_hex(4).upper()
            orders = bot.load_json(bot.ORDERS_PATH)
            orders[oid] = {
                "id": oid, "user_id": uid, "username": u.get("username", ""),
                "type": "renew" if target_un else "buy", "gb": gb, "months": months,
                "total_toman": cost, "usdt_amount": p["usdt_amount"],
                "pay_method": "wallet_miniapp", "status": "approved",
                "panel_username": uname, "sub_url": sub_url, "created_at": int(time.time()),
            }
            bot.save_json(bot.ORDERS_PATH, orders)
            try:
                bot.send_qr_with_sub(token, uid, sub_url, f"✅ <b>سرویس شما از مینی‌اپ آماده شد!</b>\n🆔 نام کاربری: <code>{html.escape(uname)}</code>\n🔗 لینک ساب:\n<code>{html.escape(sub_url)}</code>", env, uid)
            except Exception:
                pass
            return self._send_json(200, {"message": f"سرویس {uname} آماده شد", "balance": new_bal})

        if path == "/vpnapp/api/create_order":
            gb = max(1, min(5000, int(body.get("gb") or 30)))
            months = max(1, min(24, int(body.get("months") or 1)))
            target_un = str(body.get("target_username") or "").strip()
            coupon = str(body.get("coupon") or "").strip().upper()
            cpct = 0.0
            if coupon:
                cp = bot.load_json(bot.COUPONS_PATH).get(coupon)
                if cp and cp.get("active", True):
                    cpct = float(cp.get("percent", 0))
            inv_id = bot.save_invoice({
                "gb": gb,
                "months": months,
                "target_username": target_un,
                "coupon_code": coupon,
                "coupon_pct": cpct,
            })
            bot.show_invoice(
                token,
                uid,
                gb,
                months,
                env=env,
                target_username=target_un,
                coupon_code=coupon,
                coupon_pct=cpct,
                inv_id=inv_id,
            )
            return self._send_json(200, {"ok": True})

        if path == "/vpnapp/api/trial":
            bot.handle_text(token, env, {"chat": {"id": uid}, "from": u, "text": "/trial"})
            return self._send_json(200, {"message": "درخواست تست رایگان بررسی و به ربات ارسال شد"})

        if path == "/vpnapp/api/transfer":
            target_id = int(body.get("target_id") or 0)
            amt = int(body.get("amount") or 0)
            if target_id <= 0 or target_id == uid or amt < 10000:
                return self._send_json(400, {"error": "آیدی یا مبلغ نامعتبر است (حداقل ۱۰,۰۰۰ تومان)"})
            ok, new_bal = bot.adjust_wallet(uid, -amt, f"انتقال به {target_id} از مینی‌اپ")
            if not ok:
                return self._send_json(400, {"error": "موجودی کافی نیست"})
            bot.adjust_wallet(target_id, amt, f"دریافت انتقال از {uid}")
            try:
                bot.send_message(token, target_id, f"💸 مبلغ <b>{amt:,} تومان</b> از کاربر <code>{uid}</code> به کیف پول شما واریز شد.")
            except Exception:
                pass
            return self._send_json(200, {"message": f"{amt:,} تومان منتقل شد", "balance": new_bal})

        if path == "/vpnapp/api/topup_ping":
            bal = bot.get_wallet_balance(uid)
            kb = {
                "inline_keyboard": [
                    [{"text": "➕ شارژ کیف پول", "callback_data": "wallet_topup"}],
                ]
            }
            bot.send_message(token, uid, f"👛 <b>شارژ کیف پول</b>\n💵 موجودی فعلی: <b>{bal:,} تومان</b>\n\nبرای انتخاب مبلغ شارژ روی دکمه زیر بزنید:", reply_markup=kb)
            return self._send_json(200, {"ok": True})

        if path == "/vpnapp/api/tickets":
            subject = str(body.get("subject") or "").strip()[:120]
            category = str(body.get("category") or "پشتیبانی فنی").strip()[:60]
            message = str(body.get("message") or "").strip()[:2000]
            if not subject or not message:
                return self._send_json(400, {"error": "موضوع و پیام الزامی است"})
            now = int(time.time())
            tid = f"T{secrets.token_hex(3).upper()}"
            tickets = load_tickets()
            tk = {
                "id": tid, "user_id": uid, "username": u.get("username", ""),
                "subject": subject, "category": category, "status": "waiting_admin",
                "created_at": now, "updated_at": now,
                "messages": [{"sender": "user", "text": message, "ts": now}],
            }
            tickets[tid] = tk
            save_tickets(tickets)
            notify_admins_ticket(env, tk, message, is_new=True)
            return self._send_json(200, {"ticket": tk})

        if path == "/vpnapp/api/tickets/reply":
            tid = str(body.get("ticket_id") or "").strip()
            text = str(body.get("text") or "").strip()[:2000]
            if not tid or not text:
                return self._send_json(400, {"error": "متن پاسخ خالی است"})
            tickets = load_tickets()
            tk = tickets.get(tid)
            if not tk or int(tk.get("user_id", 0)) != uid:
                return self._send_json(404, {"error": "تیکت یافت نشد"})
            now = int(time.time())
            tk.setdefault("messages", []).append({"sender": "user", "text": text, "ts": now})
            tk["status"] = "waiting_admin"
            tk["updated_at"] = now
            save_tickets(tickets)
            notify_admins_ticket(env, tk, text, is_new=False)
            return self._send_json(200, {"ok": True})

        if path == "/vpnapp/api/tickets/close":
            tid = str(body.get("ticket_id") or "").strip()
            tickets = load_tickets()
            tk = tickets.get(tid)
            if not tk or int(tk.get("user_id", 0)) != uid:
                return self._send_json(404, {"error": "تیکت یافت نشد"})
            tk["status"] = "closed"
            tk["updated_at"] = int(time.time())
            save_tickets(tickets)
            return self._send_json(200, {"ok": True})

        self._send_json(404, {"error": "Not found"})


def main():
    srv = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), MiniAppHandler)
    log.info("MiniApp server listening on %s:%s", LISTEN_HOST, LISTEN_PORT)
    srv.serve_forever()


if __name__ == "__main__":
    main()
