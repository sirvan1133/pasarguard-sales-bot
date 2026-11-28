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
DEMO_ENV_PATH = Path("/opt/sales-bot-demo/.env")
LISTEN_HOST = os.environ.get("MINIAPP_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("MINIAPP_PORT", "8765"))
DEMO_GREETED_UIDS: set[int] = set()


def load_demo_token(env: dict[str, str]) -> str:
    tok = (env.get("DEMO_BOT_TOKEN") or "").strip()
    if tok:
        return tok
    if DEMO_ENV_PATH.exists():
        for line in DEMO_ENV_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("BOT_TOKEN="):
                return line.split("=", 1)[1].strip()
    return ""


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
        if ln <= 0 or ln > 8 * 1024 * 1024:
            return {}
        return json.loads(self.rfile.read(ln).decode("utf-8"))

    def _auth_user(self, env: dict[str, str]) -> tuple[dict, bool] | tuple[None, bool]:
        init_data = self.headers.get("X-Tg-Init-Data", "")
        u = verify_tg_init_data(init_data, env["BOT_TOKEN"])
        if u and u.get("id"):
            return u, False
        demo_tok = load_demo_token(env)
        if demo_tok:
            u2 = verify_tg_init_data(init_data, demo_tok)
            if u2 and u2.get("id"):
                return u2, True
        self._send_json(401, {"error": "احراز هویت تلگرام نامعتبر است. لطفاً از داخل ربات باز کنید."})
        return None, False

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

        if path in ("/vpnapp/streisand.svg", "/vpnapp/v2box.png", "/vpnapp/v2rayng.png"):
            asset_p = WEBAPP_HTML.parent / path.rsplit("/", 1)[-1]
            if not asset_p.exists():
                self.send_error(404)
                return
            data = asset_p.read_bytes()
            ctype = "image/svg+xml" if asset_p.suffix == ".svg" else "image/png"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(data)
            return

        if path == "/vpnapp/go":
            qs = urllib.parse.parse_qs(parsed.query)
            app = (qs.get("app") or [""])[0].strip().lower()
            raw_url = (qs.get("url") or [""])[0].strip()
            if not raw_url.startswith(("http://", "https://")):
                self.send_error(400)
                return
            enc = urllib.parse.quote(raw_url, safe="")
            schemes = {
                "hiddify": f"hiddify://import/{raw_url}",
                "v2rayng": f"v2rayng://install-config?url={enc}",
                "streisand": f"streisand://import/{raw_url}",
                "v2box": f"v2box://install-sub?url={enc}&name=VPN_VIP",
            }
            target = schemes.get(app)
            if not target:
                self.send_error(400)
                return
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
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
        u, is_demo = self._auth_user(env)
        if not u:
            return
        uid = int(u["id"])

        if path == "/vpnapp/api/bootstrap":
            cfg = bot.get_settings(env)
            if is_demo:
                import random
                if uid not in DEMO_GREETED_UIDS:
                    DEMO_GREETED_UIDS.add(uid)
                    try:
                        demo_env = dict(env)
                        demo_env["DEMO_MODE"] = "1"
                        demo_env["BOT_TOKEN"] = load_demo_token(env)
                        bot.send_message(demo_env["BOT_TOKEN"], uid, "📌 <b>منوی دسترسی سریع:</b>", reply_markup=bot.build_home_inline_keyboard(uid, demo_env))
                    except Exception:
                        pass
                return self._send_json(200, {
                    "user": {
                        "id": uid,
                        "first_name": u.get("first_name", "") + " (حالت تست)",
                        "username": u.get("username", ""),
                        "balance": random.choice([850000, 1250000, 2400000]),
                        "is_admin": True,
                        "is_demo": True,
                    },
                    "settings": {"gb_options": cfg.get("gb_options", bot.DEFAULT_GB_OPTIONS)},
                    "payment": {
                        "card_number": "6037-99**-****-1234",
                        "card_owner": "حساب نمایشی تست",
                        "usdt_bep20": "0xDEMO00000000000000000000000000000000DEMO",
                    },
                    "subs": [
                        {
                            "username": f"demo_{uid % 10000}",
                            "sub_url": "https://demo-sub.example.com/sub/demo12345",
                            "limit_gb": 50,
                            "used_gb": round(random.uniform(6.5, 38.0), 2),
                            "status": "active",
                            "expire_text": "2027-01-15",
                        }
                    ],
                    "tickets": get_user_tickets(uid),
                    "reseller": {
                        "is_reseller": False,
                        "price_per_gb": 5000,
                        "min_gb": 500,
                        "min_toman": 2500000,
                        "max_acc_gb": 200,
                        "accounts_created": 0,
                        "total_gb_sold": 0,
                    },
                    "admin_data": {
                        "users_count": random.randint(420, 1850),
                        "pending_count": random.randint(1, 6),
                        "approved_count": random.randint(310, 1290),
                        "total_toman": random.randint(45, 290) * 1000000,
                        "settings": {
                            "base_price_per_gb": 15000,
                            "pct_under_threshold": 2.0,
                            "pct_over_threshold": 1.0,
                            "bulk_threshold_gb": 100,
                            "renew_discount_pct": 10,
                            "global_discount_pct": 5,
                            "referral_reward_pct": 20,
                            "gb_options": "20,30,50,100,200,400,1000",
                            "trial_enabled": True,
                            "trial_gb": 1,
                            "trial_hours": 24,
                            "trial_cooldown_days": 7,
                            "force_join_enabled": False,
                            "force_join_channel": "@demo_channel",
                            "reminder_days": 3,
                            "reminder_pct": 10,
                            "card_number": "6037-99**-****-1234",
                            "card_owner": "حساب نمایشی تست",
                            "usdt_bep20": "0xDEMO00000000000000000000000000000000DEMO",
                            "card_instant_mode": "all",
                            "max_pending_orders": 2,
                            "pending_alert_minutes": 30,
                            "reseller_price_per_gb": 5000,
                            "reseller_min_gb": 500,
                            "reseller_max_gb_per_acc": 200,
                            "menu_mode": "inline",
                            "maintenance_mode": False,
                            "maintenance_msg": "🛠 ربات در حال بروزرسانی است.",
                            "backup_enabled": False,
                            "backup_interval_hours": 24,
                            "backup_chat_id": "",
                        },
                        "pending_orders": [
                            {"id": "DEMO1A", "user_id": 91001122, "gb": 50, "months": 1, "total_toman": 750000, "pay_method": "card_miniapp"},
                            {"id": "DEMO2B", "user_id": 82003344, "gb": 100, "months": 3, "total_toman": 1530000, "pay_method": "usdt"},
                        ],
                        "open_tickets": [
                            {"id": "TDEMO1", "user_id": 91001122, "subject": "تغییر سرور به آلمان", "category": "پشتیبانی فنی", "status": "waiting_admin", "messages": [{"sender": "user", "text": "سلام لطفاً کانفیگ آلمان بفرستید", "time_text": "12:10"}]},
                        ],
                        "coupons": {"WELCOME20": {"percent": 20, "active": True}, "VIP10": {"percent": 10, "active": True}},
                    },
                    "referral_link": f"https://t.me/pasarguardsalesbot?start=ref_{uid}",
                })
            bot_u = env.get("BOT_USERNAME") or "vpnnnVipbot"
            with bot.users_file_lock(bot.USERS_PATH):
                users = bot.load_json(bot.USERS_PATH)
                urec = users.get(str(uid), {})
                need_greet = not urec.get("miniapp_greeted")
                if need_greet:
                    urec = users.setdefault(str(uid), {"first_seen": int(time.time()), "last_seen": int(time.time()), "username": u.get("username", "")})
                    urec["miniapp_greeted"] = True
                    bot.save_json(bot.USERS_PATH, users)
            if need_greet:
                try:
                    bot.send_message(env["BOT_TOKEN"], uid, "📌 <b>منوی دسترسی سریع:</b>", reply_markup=bot.build_home_inline_keyboard(uid, env))
                except Exception:
                    pass
            is_adm = uid in bot.get_admin_ids(env)
            adm_data = None
            if is_adm:
                all_orders = bot.load_json(bot.ORDERS_PATH)
                pend_list = [o for o in all_orders.values() if o.get("status") == "pending"]
                pend_list.sort(key=lambda x: int(x.get("created_at") or 0), reverse=True)
                appr_list = [o for o in all_orders.values() if o.get("status") == "approved"]
                all_tks = [t for t in load_tickets().values() if t.get("status") != "closed"]
                all_tks.sort(key=lambda x: int(x.get("updated_at") or 0), reverse=True)
                for t in all_tks:
                    for m in t.get("messages", []):
                        m["time_text"] = fmt_ts(int(m.get("ts", 0)))
                adm_data = {
                    "users_count": len(bot.load_json(bot.USERS_PATH)),
                    "pending_count": len(pend_list),
                    "approved_count": len(appr_list),
                    "total_toman": sum(int(o.get("total_toman") or 0) for o in appr_list),
                    "settings": {
                        "base_price_per_gb": int(cfg.get("base_price_per_gb", 15000)),
                        "pct_under_threshold": float(cfg.get("pct_under_threshold", 2.0)),
                        "pct_over_threshold": float(cfg.get("pct_over_threshold", 1.0)),
                        "bulk_threshold_gb": int(cfg.get("bulk_threshold_gb", 100)),
                        "renew_discount_pct": float(cfg.get("renew_discount_pct", 10)),
                        "global_discount_pct": float(cfg.get("global_discount_pct", 0)),
                        "referral_reward_pct": float(cfg.get("referral_reward_pct", 20)),
                        "gb_options": ",".join(str(x) for x in cfg.get("gb_options", bot.DEFAULT_GB_OPTIONS)),
                        "trial_enabled": bool(cfg.get("trial_enabled", True)),
                        "trial_gb": int(cfg.get("trial_gb", 1)),
                        "trial_hours": int(cfg.get("trial_hours", 24)),
                        "trial_cooldown_days": int(cfg.get("trial_cooldown_days", 7)),
                        "force_join_enabled": bool(cfg.get("force_join_enabled", False)),
                        "force_join_channel": str(cfg.get("force_join_channel", "")),
                        "reminder_days": int(cfg.get("reminder_days", 3)),
                        "reminder_pct": int(cfg.get("reminder_pct", 10)),
                        "card_number": str(cfg.get("card_number", "")),
                        "card_owner": str(cfg.get("card_owner", "")),
                        "usdt_bep20": str(cfg.get("usdt_bep20", "")),
                        "card_instant_mode": str(cfg.get("card_instant_mode", "all")),
                        "max_pending_orders": int(cfg.get("max_pending_orders", 2)),
                        "pending_alert_minutes": int(cfg.get("pending_alert_minutes", 30)),
                        "reseller_price_per_gb": int(cfg.get("reseller_price_per_gb", 5000)),
                        "reseller_min_gb": int(cfg.get("reseller_min_gb", 500)),
                        "reseller_max_gb_per_acc": int(cfg.get("reseller_max_gb_per_acc", 200)),
                        "menu_mode": str(cfg.get("menu_mode", "inline")),
                        "maintenance_mode": bool(cfg.get("maintenance_mode", False)),
                        "maintenance_msg": str(cfg.get("maintenance_msg", "")),
                        "backup_enabled": bool(cfg.get("backup_enabled", True)),
                        "backup_interval_hours": int(cfg.get("backup_interval_hours", 24)),
                        "backup_chat_id": str(cfg.get("backup_chat_id", "")),
                    },
                    "pending_orders": pend_list[:15],
                    "open_tickets": all_tks[:15],
                    "coupons": bot.load_json(bot.COUPONS_PATH),
                }
            self._send_json(200, {
                "user": {
                    "id": uid,
                    "first_name": u.get("first_name", ""),
                    "username": u.get("username", ""),
                    "balance": bot.get_wallet_balance(uid),
                    "is_admin": is_adm,
                    "is_demo": False,
                },
                "settings": {"gb_options": cfg.get("gb_options", bot.DEFAULT_GB_OPTIONS)},
                "payment": {
                    "card_number": str(cfg.get("card_number") or ""),
                    "card_owner": str(cfg.get("card_owner") or ""),
                    "usdt_bep20": str(cfg.get("usdt_bep20") or ""),
                },
                "subs": fetch_user_subs(env, uid),
                "tickets": get_user_tickets(uid),
                "reseller": {
                    "is_reseller": bool(bot.get_reseller(uid)),
                    "price_per_gb": int(bot.get_reseller_price_per_gb(uid, env) if bot.get_reseller(uid) else cfg.get("reseller_price_per_gb", 5000)),
                    "min_gb": int(cfg.get("reseller_min_gb", 500)),
                    "min_toman": int(cfg.get("reseller_min_gb", 500)) * int(cfg.get("reseller_price_per_gb", 5000)),
                    "max_acc_gb": int(cfg.get("reseller_max_gb_per_acc", 200)),
                    "accounts_created": int((bot.get_reseller(uid) or {}).get("accounts_created", 0)),
                    "total_gb_sold": int((bot.get_reseller(uid) or {}).get("total_gb_sold", 0)),
                },
                "admin_data": adm_data,
                "referral_link": f"https://t.me/{bot_u}?start=ref_{uid}",
            })
            return

        if path == "/vpnapp/api/tickets":
            self._send_json(200, {"tickets": get_user_tickets(uid)})
            return

        self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        try:
            self._handle_post()
        except Exception as e:
            log.exception("Unhandled POST error")
            self._send_json(500, {"error": f"خطای داخلی سرور: {e}"})

    def _handle_post(self):
        env = bot.load_env()
        u, is_demo = self._auth_user(env)
        if not u:
            return
        uid = int(u["id"])
        token = load_demo_token(env) if is_demo else env["BOT_TOKEN"]
        path = urllib.parse.urlparse(self.path).path
        body = self._read_json()

        if path == "/vpnapp/api/send_menu":
            target_env = dict(env)
            if is_demo:
                target_env["DEMO_MODE"] = "1"
                target_env["BOT_TOKEN"] = token
            bot.send_message(token, uid, "📌 <b>منوی دسترسی سریع:</b>", reply_markup=bot.build_home_inline_keyboard(uid, target_env))
            return self._send_json(200, {"ok": True})

        if path.startswith("/vpnapp/api/admin/"):
            if is_demo:
                return self._send_json(400, {"error": "🧪 این ربات در حالت تست و نمایشی است و تغییر تنظیمات ادمین غیرفعال است."})
            if uid not in bot.get_admin_ids(env):
                return self._send_json(403, {"error": "دسترسی ادمین ندارید"})
            if path == "/vpnapp/api/admin/save_settings":
                cur_cfg = bot.load_json(bot.SETTINGS_PATH)
                int_fields = ("base_price_per_gb", "bulk_threshold_gb", "trial_gb", "trial_hours", "trial_cooldown_days", "reminder_days", "reminder_pct", "max_pending_orders", "pending_alert_minutes", "reseller_price_per_gb", "reseller_min_gb", "reseller_max_gb_per_acc", "backup_interval_hours")
                float_fields = ("pct_under_threshold", "pct_over_threshold", "renew_discount_pct", "global_discount_pct", "referral_reward_pct")
                bool_fields = ("trial_enabled", "force_join_enabled", "maintenance_mode", "backup_enabled")
                str_fields = ("force_join_channel", "card_number", "card_owner", "usdt_bep20", "card_instant_mode", "menu_mode", "maintenance_msg", "backup_chat_id")
                for k in int_fields:
                    if k in body and body[k] != "":
                        cur_cfg[k] = int(body[k])
                for k in float_fields:
                    if k in body and body[k] != "":
                        cur_cfg[k] = float(body[k])
                for k in bool_fields:
                    if k in body:
                        cur_cfg[k] = bool(body[k])
                for k in str_fields:
                    if k in body:
                        cur_cfg[k] = str(body[k]).strip()
                if "gb_options" in body:
                    opts = [int(x.strip()) for x in str(body["gb_options"]).split(",") if x.strip().isdigit() and int(x.strip()) > 0]
                    if opts:
                        cur_cfg["gb_options"] = opts
                bot.save_json(bot.SETTINGS_PATH, cur_cfg)
                return self._send_json(200, {"message": "✅ تمام تنظیمات با موفقیت ذخیره و اعمال شد"})
            if path == "/vpnapp/api/admin/wallet_adjust":
                t_uid = int(body.get("user_id") or 0)
                delta = int(body.get("delta") or 0)
                if t_uid <= 0 or delta == 0:
                    return self._send_json(400, {"error": "آیدی یا مبلغ نامعتبر است"})
                ok, new_b = bot.adjust_wallet(t_uid, delta, f"تنظیم دستی ادمین از مینی‌اپ")
                if not ok:
                    return self._send_json(400, {"error": "خطا در تغییر موجودی"})
                return self._send_json(200, {"message": f"✅ موجودی کاربر {t_uid} به {new_b:,} تومان تغییر یافت"})
            if path == "/vpnapp/api/admin/broadcast":
                msg_txt = str(body.get("text") or "").strip()[:3500]
                if not msg_txt:
                    return self._send_json(400, {"error": "متن پیام همگانی خالی است"})
                users = bot.load_json(bot.USERS_PATH)
                sent_ok = 0
                for k in list(users.keys()):
                    if k.isdigit():
                        try:
                            bot.send_message(token, int(k), f"📢 <b>اطلاعیه مدیریت:</b>\n\n{html.escape(msg_txt)}")
                            sent_ok += 1
                        except Exception:
                            pass
                return self._send_json(200, {"message": f"📢 پیام همگانی به {sent_ok} کاربر ارسال شد"})
            if path == "/vpnapp/api/admin/order_action":
                oid = str(body.get("order_id") or "").strip()
                act = str(body.get("action") or "").strip()
                if act not in ("ok", "no") or not oid:
                    return self._send_json(400, {"error": "درخواست نامعتبر"})
                orders = bot.load_json(bot.ORDERS_PATH)
                o = orders.get(oid)
                if not o or o.get("status") != "pending":
                    return self._send_json(404, {"error": "سفارش معلق یافت نشد"})
                if act == "no":
                    o["status"] = "rejected"
                    bot.save_json(bot.ORDERS_PATH, orders)
                    try:
                        bot.send_message(token, int(o["user_id"]), f"❌ متأسفانه سفارش <code>#{oid}</code> توسط مدیریت رد شد.")
                    except Exception:
                        pass
                    return self._send_json(200, {"message": f"❌ سفارش #{oid} رد شد"})
                target_un = o.get("target_username") or ""
                if target_un:
                    pu = bot.renew_panel_user(env, target_un, int(o["gb"]), int(o["months"]))
                    uname, sub_url = target_un, pu.get("subscription_url") or ""
                else:
                    pu = bot.create_panel_user(env, oid, int(o["user_id"]), int(o["gb"]), int(o["months"]), o.get("pay_method", "card"))
                    uname, sub_url = pu["username"], pu.get("subscription_url") or ""
                o["status"] = "approved"
                o["panel_username"] = uname
                o["sub_url"] = sub_url
                bot.save_json(bot.ORDERS_PATH, orders)
                try:
                    bot.send_qr_with_sub(token, int(o["user_id"]), sub_url, f"✅ <b>سفارش #{oid} تایید و فعال شد!</b>\n🆔 نام کاربری: <code>{html.escape(uname)}</code>\n🔗 لینک ساب:\n<code>{html.escape(sub_url)}</code>", env, int(o["user_id"]))
                except Exception:
                    pass
                return self._send_json(200, {"message": f"✅ سفارش #{oid} تایید و سرویس {uname} ساخته شد"})
            if path == "/vpnapp/api/admin/ticket_reply":
                tid = str(body.get("ticket_id") or "").strip()
                text = str(body.get("text") or "").strip()[:2000]
                tickets = load_tickets()
                tk = tickets.get(tid)
                if not tk or not text:
                    return self._send_json(404, {"error": "تیکت یا متن نامعتبر است"})
                now = int(time.time())
                tk.setdefault("messages", []).append({"sender": "admin", "text": text, "ts": now})
                tk["status"] = "answered"
                tk["updated_at"] = now
                save_tickets(tickets)
                try:
                    bot.send_message(token, int(tk["user_id"]), f"🎫 <b>پاسخ پشتیبانی به تیکت <code>#{tid}</code></b>\n📌 {html.escape(tk['subject'])}\n\n💬 {html.escape(text)}")
                except Exception:
                    pass
                return self._send_json(200, {"message": "✅ پاسخ ارسال شد"})
            if path == "/vpnapp/api/admin/coupon_add":
                code = str(body.get("code") or "").strip().upper()
                pct = float(body.get("percent") or 0)
                if not code or pct <= 0 or pct > 95:
                    return self._send_json(400, {"error": "کد تخفیف یا درصد (۱ تا ۹۵) نامعتبر است"})
                cps = bot.load_json(bot.COUPONS_PATH)
                cps[code] = {"percent": pct, "active": True, "created_at": int(time.time())}
                bot.save_json(bot.COUPONS_PATH, cps)
                return self._send_json(200, {"message": f"🎟 کد تخفیف {code} ({pct}٪) ثبت شد"})
            if path == "/vpnapp/api/admin/coupon_del":
                code = str(body.get("code") or "").strip().upper()
                cps = bot.load_json(bot.COUPONS_PATH)
                if code in cps:
                    del cps[code]
                    bot.save_json(bot.COUPONS_PATH, cps)
                return self._send_json(200, {"message": f"🗑 کد تخفیف {code} حذف شد"})
            return self._send_json(404, {"error": "Not found"})

        if is_demo and path in ("/vpnapp/api/buy_wallet", "/vpnapp/api/submit_receipt", "/vpnapp/api/trial", "/vpnapp/api/transfer", "/vpnapp/api/topup_ping", "/vpnapp/api/reseller_apply", "/vpnapp/api/reseller_create"):
            return self._send_json(200, {"ok": True, "message": "🧪 این عملیات در حالت تست و نمایشی شبیه‌سازی شد (تغییری در سرور واقعی ثبت نشد)."})

        if path == "/vpnapp/api/reseller_apply":
            cfg = bot.get_settings(env)
            bal = bot.get_wallet_balance(uid)
            min_gb = int(cfg.get("reseller_min_gb", 500))
            min_tmn = min_gb * int(cfg.get("reseller_price_per_gb", 5000))
            if bal < min_tmn:
                return self._send_json(400, {"error": f"ابتدا باید حداقل {min_tmn:,} تومان ({min_gb} گیگ) کیف پول خود را شارژ کنید"})
            uname_tg = u.get("username") or "-"
            kb = {
                "inline_keyboard": [[
                    {"text": "✅ تایید و فعال‌سازی نماینده", "callback_data": f"adm_res_ok:{uid}"},
                    {"text": "❌ رد درخواست", "callback_data": f"adm_res_no:{uid}"},
                ]]
            }
            for adm_id in bot.get_admin_ids(env):
                try:
                    bot.send_message(token, adm_id, f"💼 <b>درخواست پنل نمایندگی (از مینی‌اپ)</b>\n\n👤 کاربر: <code>{uid}</code> (@{html.escape(uname_tg)})\n👛 موجودی: <b>{bal:,} تومان</b>", reply_markup=kb)
                except Exception:
                    pass
            return self._send_json(200, {"message": "✅ درخواست نمایندگی شما برای مدیریت ارسال شد"})

        if path == "/vpnapp/api/reseller_create":
            rec = bot.get_reseller(uid)
            if not rec:
                return self._send_json(403, {"error": "دسترسی نمایندگی ندارید"})
            cfg = bot.get_settings(env)
            gb = max(1, int(body.get("gb") or 30))
            months = max(1, min(12, int(body.get("months") or 1)))
            max_acc = int(cfg.get("reseller_max_gb_per_acc", 200))
            if max_acc > 0 and gb > max_acc:
                return self._send_json(400, {"error": f"حداکثر حجم هر اکانت نماینده {max_acc} گیگ است"})
            ppg = bot.get_reseller_price_per_gb(uid, env)
            cost = gb * ppg
            ok_d, new_b = bot.adjust_wallet(uid, -cost, f"ساخت اکانت نماینده از مینی‌اپ ({gb}GB/{months}M)")
            if not ok_d:
                return self._send_json(400, {"error": "موجودی کیف پول کافی نیست"})
            oid = secrets.token_hex(4).upper()
            try:
                pu = bot.create_panel_user(env, oid, uid, gb, months, "reseller_miniapp")
                uname, sub_url = pu["username"], pu.get("subscription_url") or ""
            except Exception as e:
                bot.adjust_wallet(uid, cost, "برگشت وجه خطای ساخت نماینده")
                return self._send_json(500, {"error": f"خطا در پنل: {e}"})
            bot.record_reseller_sale(uid, gb, count=1)
            orders = bot.load_json(bot.ORDERS_PATH)
            orders[oid] = {
                "id": oid, "user_id": uid, "username": u.get("username", ""),
                "type": "reseller_single", "gb": gb, "months": months,
                "total_toman": cost, "usdt_amount": 0,
                "pay_method": "reseller_miniapp", "status": "approved",
                "panel_username": uname, "sub_url": sub_url, "created_at": int(time.time()),
            }
            bot.save_json(bot.ORDERS_PATH, orders)
            try:
                bot.send_qr_with_sub(token, uid, sub_url, f"✅ <b>اکانت نمایندگی آماده شد!</b>\n🆔 <code>{html.escape(uname)}</code>\n🔗 <code>{html.escape(sub_url)}</code>", env, uid)
            except Exception:
                pass
            return self._send_json(200, {"message": f"✅ اکانت {uname} ساخته شد (موجودی: {new_b:,} ت)"})

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
            oid = secrets.token_hex(4).upper()
            try:
                if target_un:
                    pu = bot.renew_panel_user(env, target_un, gb, months)
                    sub_url = pu.get("subscription_url") or ""
                    uname = target_un
                else:
                    pu = bot.create_panel_user(env, oid, uid, gb, months, "wallet_miniapp")
                    uname = pu["username"]
                    sub_url = pu.get("subscription_url") or ""
            except Exception as e:
                bot.adjust_wallet(uid, cost, "برگشت وجه خطای ساخت از مینی‌اپ")
                return self._send_json(500, {"error": f"خطا در پنل: {e}"})
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

        if path == "/vpnapp/api/submit_receipt":
            import base64
            gb = max(1, min(5000, int(body.get("gb") or 30)))
            months = max(1, min(24, int(body.get("months") or 1)))
            target_un = str(body.get("target_username") or "").strip()
            coupon = str(body.get("coupon") or "").strip().upper()
            method = str(body.get("method") or "card")
            note = str(body.get("note") or "").strip()[:1000]
            photo_b64 = str(body.get("photo_b64") or "").strip() if method == "card" else ""
            if method == "usdt" and not note:
                return self._send_json(400, {"error": "لطفاً هش (0x...) یا لینک تراکنش را وارد کنید"})
            if method == "card" and not note and not photo_b64:
                return self._send_json(400, {"error": "لطفاً عکس رسید یا کد پیگیری را وارد کنید"})
            cpct = 0.0
            if coupon:
                cp = bot.load_json(bot.COUPONS_PATH).get(coupon)
                if cp and cp.get("active", True):
                    cpct = float(cp.get("percent", 0))
            p = bot.calc_price(gb, months, env=env, is_renew=bool(target_un), coupon_pct=cpct)
            if method == "usdt" and body.get("exact_usdt"):
                try:
                    p["usdt_amount"] = round(float(body["exact_usdt"]), 6)
                except Exception:
                    pass
            oid = secrets.token_hex(4).upper()
            orders = bot.load_json(bot.ORDERS_PATH)
            if method == "usdt":
                import re
                m_tx = re.search(r"0x[a-fA-F0-9]{64}", note)
                if not m_tx:
                    return self._send_json(400, {"error": "لطفاً هش معتبر تراکنش BEP20 (شروع با 0x...) را وارد کنید"})
                tx_hash = m_tx.group(0).lower()
                if any(str(o.get("tx_hash") or "").lower() == tx_hash for o in orders.values()):
                    return self._send_json(400, {"error": "این تراکنش قبلاً ثبت و استفاده شده است"})
                cfg = bot.get_settings(env)
                ok_tx, reason_tx, _ = bot.verify_bsc_usdt_tx(tx_hash, str(cfg.get("usdt_bep20") or ""), float(p["usdt_amount"]))
                if ok_tx:
                    try:
                        if target_un:
                            pu = bot.renew_panel_user(env, target_un, gb, months)
                            uname, sub_url = target_un, pu.get("subscription_url") or ""
                        else:
                            pu = bot.create_panel_user(env, oid, uid, gb, months, "usdt_miniapp")
                            uname, sub_url = pu["username"], pu.get("subscription_url") or ""
                    except Exception as e:
                        return self._send_json(500, {"error": f"تراکنش تایید شد اما خطای پنل رخ داد: {e}"})
                    orders[oid] = {
                        "id": oid, "user_id": uid, "username": u.get("username", ""),
                        "type": "renew" if target_un else "buy", "target_username": target_un,
                        "gb": gb, "months": months, "total_toman": int(p["total_toman"]),
                        "usdt_amount": p["usdt_amount"], "pay_method": "usdt_miniapp",
                        "status": "approved", "tx_hash": tx_hash,
                        "panel_username": uname, "sub_url": sub_url, "created_at": int(time.time()),
                    }
                    bot.save_json(bot.ORDERS_PATH, orders)
                    try:
                        bot.send_qr_with_sub(token, uid, sub_url, f"✅ <b>پرداخت تتر تایید و سرویس فعال شد!</b>\n🆔 نام کاربری: <code>{html.escape(uname)}</code>\n🔗 لینک ساب:\n<code>{html.escape(sub_url)}</code>", env, uid)
                    except Exception:
                        pass
                    return self._send_json(200, {"ok": True, "auto_approved": True, "order_id": oid, "message": f"✅ تراکنش تایید و سرویس {uname} فوری فعال شد!"})
                if "شبکه" not in reason_tx and "خطا" not in reason_tx:
                    return self._send_json(400, {"error": f"❌ {reason_tx}"})
            orders[oid] = {
                "id": oid,
                "user_id": uid,
                "username": u.get("username", ""),
                "type": "renew" if target_un else "buy",
                "target_username": target_un,
                "gb": gb,
                "months": months,
                "total_toman": int(p["total_toman"]),
                "usdt_amount": p["usdt_amount"],
                "pay_method": f"{method}_miniapp",
                "status": "pending",
                "note": note,
                "created_at": int(time.time()),
            }
            bot.save_json(bot.ORDERS_PATH, orders)
            type_lbl = f"🔄 تمدید (<code>{html.escape(target_un)}</code>)" if target_un else "🛒 خرید جدید (مینی‌اپ)"
            m_lbl = "🪙 تتر (BEP20)" if method == "usdt" else "💳 کارت‌به‌کارت"
            cap = (
                f"🆕 <b>سفارش جدید #{oid}</b>\n\n"
                f"📌 نوع: {type_lbl}\n"
                f"👤 کاربر: <code>{uid}</code> (@{html.escape(u.get('username') or '-')})\n"
                f"📦 پلن: <b>{gb} گیگ / {months} ماه</b>\n"
                f"💰 روش: {m_lbl}\n"
                f"💵 مبلغ: <b>{p['total_toman']:,} تومان</b> (<code>{p['usdt_amount']} USDT</code>)"
            )
            if note:
                cap += f"\n\n📝 پیام/کد پیگیری:\n<code>{html.escape(note)}</code>"
            kb = {
                "inline_keyboard": [[
                    {"text": "✅ تایید و ساخت خودکار", "callback_data": f"adm_ok:{oid}", "style": "success"},
                    {"text": "❌ رد سفارش", "callback_data": f"adm_no:{oid}", "style": "danger"},
                ]]
            }
            tmp_p = None
            if photo_b64 and method == "card":
                try:
                    raw_b64 = photo_b64.split(",", 1)[-1]
                    img_bytes = base64.b64decode(raw_b64)
                    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
                        tf.write(img_bytes)
                        tmp_p = Path(tf.name)
                except Exception:
                    tmp_p = None
            if photo_b64:
                try:
                    raw_b64 = photo_b64.split(",", 1)[-1]
                    img_bytes = base64.b64decode(raw_b64)
                    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
                        tf.write(img_bytes)
                        tmp_p = Path(tf.name)
                except Exception:
                    tmp_p = None
            if photo_b64 and method == "card":
                try:
                    raw_b64 = photo_b64.split(",", 1)[-1]
                    img_bytes = base64.b64decode(raw_b64)
                    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
                        tf.write(img_bytes)
                        tmp_p = Path(tf.name)
                except Exception:
                    tmp_p = None
            try:
                for adm_id in bot.get_admin_ids(env):
                    try:
                        if tmp_p is not None:
                            bot.tg_send_photo(token, adm_id, tmp_p, caption=cap, reply_markup=kb)
                        else:
                            bot.send_message(token, adm_id, cap, reply_markup=kb)
                    except Exception:
                        pass
            finally:
                if tmp_p is not None:
                    tmp_p.unlink(missing_ok=True)
            return self._send_json(200, {"ok": True, "order_id": oid, "message": f"سفارش #{oid} ثبت و برای تایید ارسال شد"})

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
