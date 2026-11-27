import os
import json
import time
import math
import datetime
import requests
import pyotp
from SmartApi import SmartConnect

API_KEY = os.environ["ANGEL_API_KEY"]
CLIENT_ID = os.environ["ANGEL_CLIENT_ID"]
PIN = os.environ["ANGEL_PIN"]
TOTP_SECRET = os.environ["ANGEL_TOTP_SECRET"]
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

STATE_FILE = "bot_state.json"

INDICES = {
    "NIFTY": {
        "name": "NIFTY 50",
        "exchange": "NSE",
        "opt_exchange": "NFO",
        "symbol": "Nifty 50",
        "token": "99926000",
        "step": 50,
        "target": 120.0,
        "sl": 60.0,
        "trail_step": 80.0
    },
    "SENSEX": {
        "name": "SENSEX",
        "exchange": "BSE",
        "opt_exchange": "BFO",
        "symbol": "SENSEX",
        "token": "99919000",
        "step": 100,
        "target": 260.0,
        "sl": 130.0,
        "trail_step": 180.0
    }
}

def ist_now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)

def send_tg(msg):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        print("TG Error:", e)

def load_state():
    today_str = ist_now().strftime("%Y-%m-%d")
    default = {
        "date": today_str,
        "live_sent": False,
        "last_run_ts": 0,
        "active_trades": {},
        "prev_oi": {}
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("date") == today_str:
                for k in ["active_trades", "prev_oi"]:
                    if not isinstance(data.get(k), dict):
                        data[k] = {}
                data["active_trades"] = {
                    k: v for k, v in data["active_trades"].items() if isinstance(v, dict)
                }
                return data
        except Exception:
            pass
    return default

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def login_angel():
    obj = SmartConnect(api_key=API_KEY)
    totp = pyotp.TOTP(TOTP_SECRET).now()
    session = obj.generateSession(CLIENT_ID, PIN, totp)
    if not session or not session.get("status"):
        raise RuntimeError(f"Angel Login Failed: {session}")
    return obj

def get_spot(obj, idx_cfg):
    res = obj.ltpData(idx_cfg["exchange"], idx_cfg["symbol"], idx_cfg["token"])
    return float(res["data"]["ltp"])

def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def calc_greeks(spot, strike, t_years, iv, opt_type):
    if t_years <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return (0.5 if opt_type == "CE" else -0.5), 0.0
    r = 0.07
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / (iv * sqrt_t)
    delta = norm_cdf(d1) if opt_type == "CE" else norm_cdf(d1) - 1.0
    pdf_d1 = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
    gamma = pdf_d1 / (spot * iv * sqrt_t)
    return delta, gamma

def get_scrip_master():
    url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    r = requests.get(url, timeout=25)
    return r.json()

def get_nearest_expiry(scrips, name):
    today = ist_now().date()
    expiries = set()
    for s in scrips:
        if s.get("name") == name and s.get("instrumenttype") == "OPTIDX":
            exp_str = s.get("expiry", "")
            try:
                exp_dt = datetime.datetime.strptime(exp_str, "%d%b%Y").date()
                if exp_dt >= today:
                    expiries.add((exp_dt, exp_str))
            except Exception:
                pass
    if not expiries:
        return None, None
    return sorted(expiries, key=lambda x: x[0])[0]

def analyze_index(obj, scrips, key, cfg, vix, prev_oi_store):
    spot = get_spot(obj, cfg)
    step = cfg["step"]
    atm = round(spot / step) * step
    exp_dt, exp_str = get_nearest_expiry(scrips, key)
    if not exp_dt:
        return {"spot": spot, "signal": None, "heavy_reversal_to": "NEUTRAL", "t_years": 0.01, "iv": 0.14}

    strikes = [atm + i * step for i in range(-2, 3)]
    target_tokens = {}
    for s in scrips:
        if s.get("name") == key and s.get("expiry") == exp_str and s.get("instrumenttype") == "OPTIDX":
            try:
                st = int(float(s.get("strike", 0)) / 100)
            except Exception:
                continue
            if st in strikes:
                sym = s.get("symbol", "")
                otype = "CE" if sym.endswith("CE") else ("PE" if sym.endswith("PE") else None)
                if otype:
                    target_tokens[s["token"]] = {"strike": st, "type": otype, "symbol": sym}

    if not target_tokens:
        return {"spot": spot, "signal": None, "heavy_reversal_to": "NEUTRAL", "t_years": 0.01, "iv": 0.14}

    tokens_list = list(target_tokens.keys())
    try:
        mkt = obj.getMarketData("FULL", {cfg["opt_exchange"]: tokens_list})
        fetched = mkt.get("data", {}).get("fetched", [])
    except Exception as e:
        print(f"MarketData error {key}:", e)
        return {"spot": spot, "signal": None, "heavy_reversal_to": "NEUTRAL", "t_years": 0.01, "iv": 0.14}

    ce_oi_tot = 0.0
    pe_oi_tot = 0.0
    ce_oi_chg_api = 0.0
    pe_oi_chg_api = 0.0

    for item in fetched:
        tok = str(item.get("symbolToken"))
        info = target_tokens.get(tok)
        if not info:
            continue
        oi = float(item.get("opnInterest", 0) or 0)
        chg = float(item.get("netChangeOpnInterest", 0) or 0)

        if info["type"] == "CE":
            ce_oi_tot += oi
            ce_oi_chg_api += chg
        else:
            pe_oi_tot += oi
            pe_oi_chg_api += chg

    total_oi = ce_oi_tot + pe_oi_tot
    if total_oi <= 0:
        return {"spot": spot, "signal": None, "heavy_reversal_to": "NEUTRAL", "t_years": 0.01, "iv": 0.14}

    prev = prev_oi_store.get(key, {})
    if ce_oi_chg_api == 0 and pe_oi_chg_api == 0 and prev.get("atm") == atm:
        ce_oi_chg = ce_oi_tot - float(prev.get("ce_tot", ce_oi_tot))
        pe_oi_chg = pe_oi_tot - float(prev.get("pe_tot", pe_oi_tot))
    else:
        ce_oi_chg = ce_oi_chg_api
        pe_oi_chg = pe_oi_chg_api

    prev_oi_store[key] = {"atm": atm, "ce_tot": ce_oi_tot, "pe_tot": pe_oi_tot}

    pcr = pe_oi_tot / ce_oi_tot if ce_oi_tot > 0 else 1.0
    days_left = max((exp_dt - ist_now().date()).days, 0) + 0.5
    t_years = days_left / 365.0
    iv = max(vix / 100.0, 0.12)

    ce_delta_raw, _ = calc_greeks(spot, atm, t_years, iv, "CE")
    pe_delta_raw, _ = calc_greeks(spot, atm, t_years, iv, "PE")
    ce_delta = abs(ce_delta_raw)
    pe_delta = abs(pe_delta_raw)

    bullish_oi_net = pe_oi_chg - ce_oi_chg
    bearish_oi_net = ce_oi_chg - pe_oi_chg
    bullish_pct = (bullish_oi_net / total_oi) * 100.0
    bearish_pct = (bearish_oi_net / total_oi) * 100.0

    signal = None
    heavy_reversal_to = "NEUTRAL"
    reason = ""
    strike_1 = atm
    strike_2 = atm

    # 100% Pure OI + Greeks Reversal (No price indicators)
    if bullish_pct >= 3.5 and pe_oi_chg > 0 and ce_oi_chg <= 0:
        heavy_reversal_to = "CALL"
    elif bearish_pct >= 3.5 and ce_oi_chg > 0 and pe_oi_chg <= 0:
        heavy_reversal_to = "PUT"

    # 100% Pure OI + Greeks Entry: Both Total OI (PCR) AND Fresh OI Writing must agree on direction
    if pcr >= 1.15 and bullish_pct >= 2.0 and pe_oi_chg > ce_oi_chg and ce_delta >= 0.49:
        signal = "CALL"
        strike_1 = atm
        strike_2 = atm - step
        reason = f"Pure Bullish OI (PCR {round(pcr, 2)}, +{round(bullish_pct, 1)}% Net PE Writing) & Delta {round(ce_delta, 2)}."

    elif pcr <= 0.85 and bearish_pct >= 2.0 and ce_oi_chg > pe_oi_chg and pe_delta >= 0.49:
        signal = "PUT"
        strike_1 = atm
        strike_2 = atm + step
        reason = f"Pure Bearish OI (PCR {round(pcr, 2)}, +{round(bearish_pct, 1)}% Net CE Writing) & Delta {round(pe_delta, 2)}."

    return {
        "spot": spot,
        "signal": signal,
        "heavy_reversal_to": heavy_reversal_to,
        "t_years": t_years,
        "iv": iv,
        "strike_1": strike_1,
        "strike_2": strike_2,
        "reason": reason
    }

def main():
    now = ist_now()
    mins = now.hour * 60 + now.minute

    if mins < (9 * 60 + 14) or mins > (15 * 60 + 25):
        print("Outside market hours:", now)
        return

    state = load_state()
    now_ts = time.time()

    if now_ts - float(state.get("last_run_ts", 0)) < 60:
        print("Skipping duplicate concurrent run")
        return
    state["last_run_ts"] = now_ts
    save_state(state)

    obj = login_angel()

    try:
        vix_res = obj.ltpData("NSE", "INDIA VIX", "99926017")
        vix = float(vix_res["data"]["ltp"])
    except Exception:
        vix = 14.0

    scrips = get_scrip_master()
    prev_oi_store = state.setdefault("prev_oi", {})

    results = {}
    for key, cfg in INDICES.items():
        results[key] = analyze_index(obj, scrips, key, cfg, vix, prev_oi_store)

    if not state.get("live_sent"):
        n_spot = round(results["NIFTY"]["spot"], 2)
        s_spot = round(results["SENSEX"]["spot"], 2)
        send_tg(f"🟢 BOT LIVE (Pure OI+Greeks) | NIFTY: {n_spot} | SENSEX: {s_spot}")
        state["live_sent"] = True

    active = state.setdefault("active_trades", {})

    for key in list(active.keys()):
        tr = active.get(key)
        if not isinstance(tr, dict):
            del active[key]
            continue

        cfg = INDICES[key]
        res = results[key]
        spot = res["spot"]
        side = tr["side"]
        entry = tr["entry_spot"]
        tgt = tr["target_spot"]
        sl = tr["sl_spot"]
        level = tr.get("trail_level", 0)
        strike_num = int(tr.get("strike_num", round(entry / cfg["step"]) * cfg["step"]))
        pts = round(spot - entry if side == "CALL" else entry - spot, 1)

        opt_type = "CE" if side == "CALL" else "PE"
        live_delta_raw, _ = calc_greeks(spot, strike_num, res["t_years"], res["iv"], opt_type)
        live_delta = abs(live_delta_raw)

        closed = False

        if (side == "CALL" and spot <= sl) or (side == "PUT" and spot >= sl):
            if level == 0:
                send_tg(
                    f"🛑 SL HIT — {cfg['name']} {side}\n"
                    f"Strike: {tr['strike']} | Spot: {round(spot, 2)} ({pts} pts)"
                )
            else:
                send_tg(
                    f"🏁 FINAL EXIT (Trailing SL Hit) — {cfg['name']} {side}\n"
                    f"Strike: {tr['strike']} | Spot: {round(spot, 2)}\n"
                    f"💰 Locked Profit: {'+' if pts >= 0 else ''}{pts} pts"
                )
            closed = True

        elif res["heavy_reversal_to"] != "NEUTRAL" and res["heavy_reversal_to"] != side and live_delta < 0.35:
            send_tg(
                f"⚠️ TREND REVERSAL EXIT — {cfg['name']} {side}\n"
                f"Strike: {tr['strike']} | Spot: {round(spot, 2)} ({'+' if pts >= 0 else ''}{pts} pts)\n"
                f"📉 Pure OI & Delta reversed ({round(live_delta, 2)}) — exit before SL!"
            )
            closed = True

        elif (side == "CALL" and spot >= tgt) or (side == "PUT" and spot <= tgt):
            new_level = level + 1
            step_pts = cfg["trail_step"]
            if side == "CALL":
                new_tgt = round(spot + step_pts, 1)
                new_sl = round(entry, 1) if new_level == 1 else round(spot - step_pts, 1)
            else:
                new_tgt = round(spot - step_pts, 1)
                new_sl = round(entry, 1) if new_level == 1 else round(spot + step_pts, 1)

            tr["trail_level"] = new_level
            tr["target_spot"] = new_tgt
            tr["sl_spot"] = new_sl

            sl_note = "Entry/Cost (Risk-Free!)" if new_level == 1 else f"{new_sl} (Profit Locked!)"
            send_tg(
                f"🔥 TARGET {new_level} HIT — {cfg['name']} {side} (+{abs(pts)} pts)\n"
                f"Spot: {round(spot, 2)} | Pure OI & Greeks still strong!\n"
                f"🚀 Extended Target {new_level + 1}: {new_tgt}\n"
                f"🛡️ Trailing SL moved to: {sl_note}"
            )

        if closed:
            del active[key]

    if mins >= (15 * 60 + 5):
        save_state(state)
        return

    for key, cfg in INDICES.items():
        if key in active:
            continue

        res = results[key]
        sig = res["signal"]
        if not sig:
            continue

        spot = res["spot"]
        if sig == "CALL":
            tgt_spot = round(spot + cfg["target"], 1)
            sl_spot = round(spot - cfg["sl"], 1)
            strike_str = f"{res['strike_1']} CE"
            alt_str = f"{res['strike_2']} CE"
        else:
            tgt_spot = round(spot - cfg["target"], 1)
            sl_spot = round(spot + cfg["sl"], 1)
            strike_str = f"{res['strike_1']} PE"
            alt_str = f"{res['strike_2']} PE"

        msg = (
            f"🟢 {cfg['name']} — BUY {sig}\n"
            f"Spot: {round(spot, 2)}\n"
            f"👉 Strike: {strike_str} (or {alt_str})\n"
            f"🎯 Target 1: {tgt_spot} (+{cfg['target']} pts)\n"
            f"🛑 Initial SL: {sl_spot} (-{cfg['sl']} pts)\n"
            f"💡 {res['reason']}"
        )
        send_tg(msg)

        active[key] = {
            "side": sig,
            "strike": strike_str,
            "strike_num": res["strike_1"],
            "entry_spot": spot,
            "target_spot": tgt_spot,
            "sl_spot": sl_spot,
            "trail_level": 0,
            "opened_at": now_ts
        }

    save_state(state)

if __name__ == "__main__":
    main()
