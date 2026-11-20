import os
import json
import time
import pyotp
import requests
from datetime import datetime, timedelta
import pytz
from SmartApi import SmartConnect

API_KEY = os.getenv("ANGEL_API_KEY")
CLIENT_ID = os.getenv("ANGEL_CLIENT_ID")
PIN = os.getenv("ANGEL_PIN")
TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

VIX_TOKEN = "99926017"
STATE_FILE = "bot_state.json"

INDICES = {
    "NIFTY": {
        "name": "NIFTY 50",
        "token": "99926000",
        "step": 50,
        "exp_weekday": 1,   # Tuesday
        "min_move": 40.0,
        "min_sl": 20.0,
        "max_sl": 40.0
    },
    "SENSEX": {
        "name": "SENSEX",
        "token": "99919000",
        "step": 100,
        "exp_weekday": 3,   # Thursday
        "min_move": 130.0,
        "min_sl": 65.0,
        "max_sl": 120.0
    }
}

def get_ist_now():
    return datetime.now(pytz.timezone('Asia/Kolkata'))

def load_state(today_str):
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                st = json.load(f)
                if st.get("date") == today_str:
                    return st
        except Exception:
            pass
    return {
        "date": today_str,
        "started": False,
        "closed": False,
        "open_spots": {"NIFTY": 0, "SENSEX": 0},
        "active_trades": {"NIFTY": None, "SENSEX": None},
        "cooldown_until": {"NIFTY": 0, "SENSEX": 0}
    }

def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f)

def send_telegram(msg):
    if not BOT_TOKEN or not CHAT_ID:
        print(msg)
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"Telegram Error: {e}")

def connect_angel():
    obj = SmartConnect(api_key=API_KEY)
    totp = pyotp.TOTP(TOTP_SECRET).now()
    data = obj.generateSession(CLIENT_ID, PIN, totp)
    if not data.get('status'):
        raise Exception(f"Angel Login Failed: {data}")
    return obj

def fetch_live_prices(obj):
    try:
        res = obj.getMarketData("LTP", {"NSE": ["99926000", VIX_TOKEN], "BSE": ["99919000"]})
        out = {}
        if res and res.get('status') and res.get('data') and res['data'].get('fetched'):
            for item in res['data']['fetched']:
                tok = item.get('symbolToken')
                if tok == "99926000":
                    out["NIFTY"] = float(item['ltp'])
                elif tok == "99919000":
                    out["SENSEX"] = float(item['ltp'])
                elif tok == VIX_TOKEN:
                    out["VIX"] = float(item['ltp'])
        return out
    except Exception:
        return {}

def get_expiry_candidates(target_weekday):
    now = get_ist_now()
    candidates = []
    for wd in [target_weekday, 3, 1, 2, 0]:
        for i in range(0, 8):
            d = now + timedelta(days=i)
            if d.weekday() == wd:
                exp_str = d.strftime("%d%b%Y").upper()
                if exp_str not in candidates:
                    candidates.append(exp_str)
                break
    return candidates

def fetch_greek_data(obj, key, exp_weekday):
    for exp in get_expiry_candidates(exp_weekday):
        try:
            res = obj.optionGreek({"name": key, "expirydate": exp})
            if res and res.get('status') and res.get('data'):
                return res['data']
        except Exception:
            continue
    return None

def analyze_market_state(obj, key, spot, open_spot, vix):
    cfg = INDICES[key]
    step = cfg['step']
    atm = int(round(spot / step) * step)
    itm_ce = atm - step
    itm_pe = atm + step

    data = fetch_greek_data(obj, key, cfg['exp_weekday'])
    ce_vol, pe_vol = 0.0, 0.0
    greeks = {}
    max_ce_strike, max_ce_vol = None, 0.0
    max_pe_strike, max_pe_vol = None, 0.0
    atm_iv = vix if (vix and vix > 0) else 13.5

    if data:
        for item in data:
            strike = int(round(float(item.get('strikePrice', 0))))
            otype = item.get('optionType')
            vol = float(item.get('tradeVolume', 0))
            iv = float(item.get('impliedVolatility', 0))

            if abs(strike - atm) <= (step * 4):
                if otype == "CE":
                    ce_vol += vol
                elif otype == "PE":
                    pe_vol += vol

            if otype == "CE" and strike > spot and vol > max_ce_vol:
                max_ce_vol = vol
                max_ce_strike = strike
            elif otype == "PE" and strike < spot and vol > max_pe_vol:
                max_pe_vol = vol
                max_pe_strike = strike

            if strike == atm and iv > 0:
                atm_iv = iv

            if strike in [atm, itm_ce, itm_pe]:
                greeks[(strike, otype)] = {
                    "delta": abs(float(item.get('delta', 0))),
                    "iv": iv
                }

    pcr = (pe_vol / ce_vol) if ce_vol > 0 else 1.0
    move = spot - open_spot

    expected_swing = spot * (atm_iv / 100.0) / 16.0 * 0.50
    dyn_sl = round(max(cfg['min_sl'], min(expected_swing, cfg['max_sl'])), 1)

    signal = None

    if (pcr >= 1.15 and move > 0) or (move >= cfg['min_move'] and pcr >= 0.95):
        s1 = itm_ce if (vix and vix < 14.0) else atm
        s2 = atm if s1 == itm_ce else itm_ce
        g = greeks.get((s1, "CE")) or greeks.get((atm, "CE"))
        if not g or g['delta'] >= 0.45:
            sl_lvl = round(spot - dyn_sl, 1)
            rr_target = spot + (dyn_sl * 2.0)
            if max_ce_strike and (max_ce_strike - spot) >= (dyn_sl * 1.4):
                tgt_lvl = round(min(rr_target, float(max_ce_strike)), 1)
            else:
                tgt_lvl = round(rr_target, 1)
            dyn_tgt = round(tgt_lvl - spot, 1)

            signal = {
                "side": "CALL",
                "s1": f"{s1} CE",
                "s2": f"{s2} CE",
                "sl_lvl": sl_lvl,
                "tgt_lvl": tgt_lvl,
                "sl_pts": dyn_sl,
                "tgt_pts": dyn_tgt,
                "reason": "Bullish OI support & Greeks favor fast CALL premium growth."
            }

    elif (pcr <= 0.85 and move < 0) or (move <= -cfg['min_move'] and pcr <= 1.05):
        s1 = itm_pe if (vix and vix < 14.0) else atm
        s2 = atm if s1 == itm_pe else itm_pe
        g = greeks.get((s1, "PE")) or greeks.get((atm, "PE"))
        if not g or g['delta'] >= 0.45:
            sl_lvl = round(spot + dyn_sl, 1)
            rr_target = spot - (dyn_sl * 2.0)
            if max_pe_strike and (spot - max_pe_strike) >= (dyn_sl * 1.4):
                tgt_lvl = round(max(rr_target, float(max_pe_strike)), 1)
            else:
                tgt_lvl = round(rr_target, 1)
            dyn_tgt = round(spot - tgt_lvl, 1)

            signal = {
                "side": "PUT",
                "s1": f"{s1} PE",
                "s2": f"{s2} PE",
                "sl_lvl": sl_lvl,
                "tgt_lvl": tgt_lvl,
                "sl_pts": dyn_sl,
                "tgt_pts": dyn_tgt,
                "reason": "Bearish OI pressure & Greeks favor fast PUT premium growth."
            }

    return {"pcr": pcr, "signal": signal}

def run_once():
    now = get_ist_now()
    if now.weekday() >= 5:
        return

    today_str = now.strftime("%Y-%m-%d")
    st = load_state(today_str)
    mins = now.hour * 60 + now.minute

    if mins < (9 * 60 + 14):
        return

    if mins >= (15 * 60 + 25):
        if not st["closed"]:
            st["closed"] = True
            send_telegram("🔴 *Market Closed — Bot Shutting Down.*")
            save_state(st)
        return

    obj = connect_angel()
    prices = fetch_live_prices(obj)
    if not prices:
        return

    if not st["started"]:
        st["started"] = True
        st["open_spots"]["NIFTY"] = prices.get("NIFTY", 0)
        st["open_spots"]["SENSEX"] = prices.get("SENSEX", 0)
        send_telegram(
            f"🟢 *BOT LIVE* | NIFTY: `{prices.get('NIFTY')}` | SENSEX: `{prices.get('SENSEX')}`"
        )
        save_state(st)

    if mins < (9 * 60 + 20):
        return

    vix = prices.get("VIX", 0)
    now_ts = time.time()

    states = {}
    for key in INDICES:
        spot = prices.get(key)
        if spot:
            if st["open_spots"][key] == 0:
                st["open_spots"][key] = spot
            states[key] = analyze_market_state(obj, key, spot, st["open_spots"][key], vix)

    # 1. Monitor Active Trades (15-min minimum hold before Momentum Shift)
    for key, cfg in INDICES.items():
        spot = prices.get(key)
        tr = st["active_trades"][key]
        mkt = states.get(key)
        if not spot or not tr or not mkt:
            continue

        pcr = mkt['pcr']

        if tr['side'] == "CALL":
            if spot >= tr['target']:
                send_telegram(
                    f"🎯 *TARGET HIT — {cfg['name']} CALL*\n"
                    f"Strike: *{tr['s1']}* | Spot: `{spot}` (+{tr['tgt_pts']} pts) ✅"
                )
                st["active_trades"][key] = None
                st["cooldown_until"][key] = now_ts + 600
            elif spot <= tr['sl']:
                send_telegram(
                    f"🛑 *SL HIT — {cfg['name']} CALL*\n"
                    f"Strike: *{tr['s1']}* | Spot: `{spot}` (-{tr['sl_pts']} pts)"
                )
                st["active_trades"][key] = None
                st["cooldown_until"][key] = now_ts + 600
            elif (now_ts - tr['opened_at']) >= 900 and pcr <= 0.80:
                pts_diff = round(spot - tr['entry'], 1)
                send_telegram(
                    f"⚠️ *MOMENTUM SHIFT — EXIT {cfg['name']} CALL*\n"
                    f"Strike: *{tr['s1']}* | Current Spot: `{spot}` ({pts_diff:+} pts)\n"
                    f"💡 OI & Greeks reversed — exit CALL now, momentum shifting!"
                )
                st["active_trades"][key] = None
                st["cooldown_until"][key] = now_ts + 600

        elif tr['side'] == "PUT":
            if spot <= tr['target']:
                send_telegram(
                    f"🎯 *TARGET HIT — {cfg['name']} PUT*\n"
                    f"Strike: *{tr['s1']}* | Spot: `{spot}` (+{tr['tgt_pts']} pts) ✅"
                )
                st["active_trades"][key] = None
                st["cooldown_until"][key] = now_ts + 600
            elif spot >= tr['sl']:
                send_telegram(
                    f"🛑 *SL HIT — {cfg['name']} PUT*\n"
                    f"Strike: *{tr['s1']}* | Spot: `{spot}` (-{tr['sl_pts']} pts)"
                )
                st["active_trades"][key] = None
                st["cooldown_until"][key] = now_ts + 600
            elif (now_ts - tr['opened_at']) >= 900 and pcr >= 1.20:
                pts_diff = round(tr['entry'] - spot, 1)
                send_telegram(
                    f"⚠️ *MOMENTUM SHIFT — EXIT {cfg['name']} PUT*\n"
                    f"Strike: *{tr['s1']}* | Current Spot: `{spot}` ({pts_diff:+} pts)\n"
                    f"💡 OI & Greeks reversed — exit PUT now, momentum shifting!"
                )
                st["active_trades"][key] = None
                st["cooldown_until"][key] = now_ts + 600

    # 2. Evaluate New Entries with Parallel Sync
    nifty_sig = states.get("NIFTY", {}).get("signal")
    sensex_sig = states.get("SENSEX", {}).get("signal")

    conflict = (
        nifty_sig and sensex_sig and nifty_sig['side'] != sensex_sig['side']
    ) or (
        st["active_trades"]["NIFTY"] and sensex_sig and st["active_trades"]["NIFTY"]['side'] != sensex_sig['side']
    ) or (
        st["active_trades"]["SENSEX"] and nifty_sig and st["active_trades"]["SENSEX"]['side'] != nifty_sig['side']
    )

    if not conflict:
        for key, cfg in INDICES.items():
            sig = states.get(key, {}).get("signal")
            spot = prices.get(key)
            if sig and spot and st["active_trades"][key] is None and now_ts >= st["cooldown_until"][key]:
                dot = "🟢" if sig['side'] == "CALL" else "🔴"
                st["active_trades"][key] = {
                    "side": sig['side'],
                    "s1": sig['s1'],
                    "entry": spot,
                    "sl": sig['sl_lvl'],
                    "target": sig['tgt_lvl'],
                    "sl_pts": sig['sl_pts'],
                    "tgt_pts": sig['tgt_pts'],
                    "opened_at": now_ts
                }

                send_telegram(
                    f"{dot} *{cfg['name']} — BUY {sig['side']}*\n"
                    f"Spot: `{spot}`\n"
                    f"👉 Strike: *{sig['s1']}* (or `{sig['s2']}`)\n"
                    f"🎯 Spot Target: `{sig['tgt_lvl']}` (+{sig['tgt_pts']} pts)\n"
                    f"🛑 Spot SL: `{sig['sl_lvl']}` (-{sig['sl_pts']} pts)\n"
                    f"💡 {sig['reason']}"
                )

    save_state(st)

if __name__ == "__main__":
    run_once()
