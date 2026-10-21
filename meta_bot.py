import os, time, json, requests
from datetime import datetime

TOKEN = os.environ["META_TOKEN"]
AD_ACCOUNT = "act_1796438588164898"
API = "https://graph.facebook.com/v21.0"
RESULT_TYPE = "onsite_conversion.messaging_conversation_started_7d"

# ================= SETTINGS (change only these) =================
NAME_FILTER = ["AZIZA","IMAN", "HOUDA", "KAWTAR", "HANAN", "ZINEB"]   # all names: AZIZA, HANAN, IMAN, HOUDA, KAWTAR, ZINEB
START_HOUR = 10                # turn ON at 8:00
END_HOUR = 19                 # turn OFF at 19:00 (7 pm)
START_HOUR_BY_NAME = {"IMAN": 10}       # own start hour for some girls, e.g. {"HOUDA": 10} = HOUDA turns on at 10:00, the others at START_HOUR
DAILY_BUDGET = 70.0          # $ per day for all these campaigns together
MAX_COST = 0.30               # pause a campaign if cost per result is this or more
NO_RESULT_MAX_SPEND = 1.0     # pause a campaign that spent this much ($) and still has NO results
REENABLE_MAX_PER_DAY = 3      # stopped campaign whose cost falls under MAX_COST is turned on again (max times per day)
BOOST_MIN_RESULTS = 4         # budget raise needs at least this many results
BOOST_TIERS = [(0.10, 60), (0.20, 30), (0.30, 15)]  # (cost at or below, % more daily budget)
BOOST_MAX_PER_DAY = 2         # max budget raises per campaign per day
MAX_CAMPAIGN_BUDGET = 20.0    # never raise one campaign above this $
INTERVAL = 120                # seconds between checks (300 = 5 min)
DRY_RUN = False               # True = only show what it would do, False = really do it
SHOW_OFF = False              # False = hide stopped/off campaigns from the table, True = show them
DUPLICATE_MIN_RESULTS = 4     # duplicating needs at least this many results
DUPLICATE_TIERS = [(0.15, 2), (0.20, 1)]  # (cost at or below, total copies of that campaign)
DUPLICATE_MAX_PER_DAY = 4     # safety: max new campaigns the bot creates per day
DUPLICATE_STATUS = "ACTIVE"   # copies start "ACTIVE" (or "PAUSED" if you want to turn them on yourself)
DUPLICATE_TAG = "BOT COPY"    # added to the name of every copy the bot makes, with the date ("" = Meta's normal " - Copy")
REPORT_EVERY = 3600           # seconds between Telegram reports (3600 = every hour, 0 = no reports)
# ================================================================

BOOST_COOLDOWN = 3600  # seconds between two raises of the same campaign
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_state.json")
ACTION_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "actions.log")  # history of everything the bot did

os.system("")  # turns on colors in Windows cmd
RESET, BOLD = "\033[0m", "\033[1m"
RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, GREY = ("\033[91m", "\033[92m", "\033[93m",
                                                 "\033[94m", "\033[95m", "\033[96m", "\033[90m")
MARK_COLORS = {"STOP": RED, "OFF": RED, "FAIL": RED, "ON": BLUE,
               "BOOST": CYAN, "COPY": MAGENTA, "B+COPY": MAGENTA}


def paint(text, color):
    return f"{color}{text}{RESET}" if color else text


def tier_value(tiers, cost):
    """Value of the first tier whose limit is at or above the cost (cheapest tier first)."""
    for limit, value in sorted(tiers):
        if cost <= limit:
            return value
    return 0


def cost_color(r):
    c = r["cost"]
    if c is None:
        return GREY
    if c >= MAX_COST:
        return RED
    if c <= max(limit for limit, _ in DUPLICATE_TIERS):
        return GREEN
    return YELLOW


def is_bad(r):
    """Too expensive: cost per result at/over the limit, or spent NO_RESULT_MAX_SPEND with no result at all."""
    return ((r["cost"] is not None and r["cost"] >= MAX_COST)
            or (r["results"] == 0 and r["spend"] >= NO_RESULT_MAX_SPEND))


def now():
    return datetime.now()


def group_of(name):
    """Which name from NAME_FILTER this campaign belongs to (the first one that matches)."""
    for g in (NAME_FILTER if NAME_FILTER else [""]):
        if g.lower() in name.lower():
            return g
    return None


def start_hour_of(group):
    for k, v in START_HOUR_BY_NAME.items():
        if k.upper() == group.upper():
            return v
    return START_HOUR


def new_state(day, duplicated=None):
    return {"day": day, "on_done": False, "off_done": False, "budget_hit": False,
            "stopped": [], "reenabled": {}, "boosts": {}, "boost_time": {},
            "dup_today": 0, "duplicated": duplicated or {},
            "last_report": 0, "final_report": False, "on_groups": [], "budget_report": False}


def load_state():
    day = now().strftime("%Y-%m-%d")
    old = {}
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        old = s.get("duplicated", {})
        if s.get("day") == day:
            for k, v in new_state(day).items():
                s.setdefault(k, v)
            return s
    except Exception:
        pass
    return new_state(day, old)  # new day: counters reset, but "already duplicated" is remembered


def save_state(s):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(s, f)
    except Exception as e:
        print("Could not save state:", e)


def results_count(insights):
    for a in insights.get("actions", []):
        if a["action_type"] == RESULT_TYPE:
            return int(a["value"])
    return 0


def get_campaigns():
    url = f"{API}/{AD_ACCOUNT}/campaigns"
    params = {
        "fields": "name,status,daily_budget,insights.date_preset(today){spend,actions}",
        "effective_status": json.dumps(["ACTIVE", "PAUSED"]),
        "limit": 100,
        "access_token": TOKEN,
    }
    data = []
    while url:
        try:
            r = requests.get(url, params=params, timeout=30).json()
        except Exception as e:
            print("Network error:", e)
            return None
        if "error" in r:
            print("API error:", r["error"].get("message"))
            return None
        data += r.get("data", [])
        url = r.get("paging", {}).get("next")
        params = None

    rows = []
    for c in data:
        if NAME_FILTER and not any(n.lower() in c["name"].lower() for n in NAME_FILTER):
            continue
        ins = c.get("insights", {}).get("data", [])
        spend = float(ins[0]["spend"]) if ins else 0.0
        results = results_count(ins[0]) if ins else 0
        rows.append({
            "id": c["id"],
            "name": c["name"],
            "active": c.get("status") == "ACTIVE",
            "spend": spend,
            "results": results,
            "cost": spend / results if results else None,
            "budget": int(c["daily_budget"]) if c.get("daily_budget") else None,
            "mark": "",
        })
    return rows


def log_action(what, r, why, result, new_name=""):
    """One line per real action (turn on, pause, raise budget, duplicate), so the assistant can answer questions."""
    try:
        with open(ACTION_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"time": now().strftime("%Y-%m-%d %H:%M:%S"), "action": what,
                                "campaign": r["name"], "id": r["id"], "why": why, "result": result,
                                "new_name": new_name}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def duplicated_today():
    """The copies the bot really made today, from the action history."""
    out = []
    today = now().strftime("%Y-%m-%d")
    try:
        with open(ACTION_LOG, encoding="utf-8") as f:
            for ln in f:
                try:
                    a = json.loads(ln)
                except Exception:
                    continue
                if a.get("action") == "DUPLICATE" and a.get("result") == "done" and a["time"][:10] == today:
                    out.append(a)
    except Exception:
        pass
    return out


def send(r, what, why, edge="", **fields):
    """Change a campaign on Meta (edge="/copies" duplicates it). Returns True only if it really happened."""
    print(f"  -> {'WOULD ' if DRY_RUN else ''}{what}: {r['name']} ({why})")
    if DRY_RUN:
        return False
    try:
        res = requests.post(f"{API}/{r['id']}{edge}", data={**fields, "access_token": TOKEN}, timeout=60).json()
    except Exception as e:
        print("     FAILED (network):", e)
        log_action(what, r, why, "failed (network)")
        return False
    if "error" in res:
        print("     FAILED:", res["error"].get("message", res["error"]))
        log_action(what, r, why, "failed: " + str(res["error"].get("message", res["error"]))[:150])
        return False
    if edge or res.get("success"):
        new_name = ""
        if edge == "/copies":
            try:
                new_name = r["name"] + json.loads(fields["rename_options"])["rename_suffix"]
            except Exception:
                pass
        log_action(what, r, why, "done", new_name)
        return True
    print("     FAILED:", res)
    log_action(what, r, why, "failed")
    return False


def set_status(r, status, why, mark):
    ok = send(r, "TURN ON" if status == "ACTIVE" else "PAUSE", why, status=status)
    if ok or DRY_RUN:
        r["active"] = (status == "ACTIVE")
        r["mark"] = mark
    else:
        r["mark"] = "FAIL"
    return ok


def try_boost(r, state, pct):
    if r["budget"] is None:
        return
    cap = int(MAX_CAMPAIGN_BUDGET * 100)
    times = state["boosts"].get(r["id"], 0)
    last = state["boost_time"].get(r["id"], 0)
    if times >= BOOST_MAX_PER_DAY or now().timestamp() - last < BOOST_COOLDOWN:
        return
    new = min(int(r["budget"] * (1 + pct / 100)), cap)
    if new <= r["budget"]:
        return
    ok = send(r, f"RAISE BUDGET +{pct}% ${r['budget'] / 100:.2f} -> ${new / 100:.2f}",
              f"{r['results']} results at ${r['cost']:.2f}", daily_budget=new)
    if ok or DRY_RUN:
        r["mark"] = "BOOST"
    if ok:
        r["budget"] = new
        state["boosts"][r["id"]] = times + 1
        state["boost_time"][r["id"]] = now().timestamp()
        save_state(state)


def copy_suffix(n=1):
    return f" - {DUPLICATE_TAG} {now().strftime('%d/%m')} #{n}" if DUPLICATE_TAG else " - Copy"


def try_duplicate(r, state):
    want = tier_value(DUPLICATE_TIERS, r["cost"])
    have = state["duplicated"].get(r["id"], 0)
    n = min(want - have, DUPLICATE_MAX_PER_DAY - state["dup_today"])
    for i in range(max(n, 0)):
        ok = send(r, "DUPLICATE", f"cost ${r['cost']:.2f}, copy {have + i + 1} of {want}",
                  edge="/copies", deep_copy="true", status_option=DUPLICATE_STATUS,
                  rename_options=json.dumps({"rename_suffix": copy_suffix(have + i + 1)}))
        if ok or DRY_RUN:
            r["mark"] = "COPY" if r["mark"] in ("", "COPY") else "B+COPY"
        if not ok:
            if not DRY_RUN:
                break  # failed: stop trying for this campaign in this check
            continue
        state["duplicated"][r["id"]] = have + i + 1
        state["dup_today"] += 1
        save_state(state)  # saved right away so a crash can never duplicate twice


def telegram(text):
    """Send a Telegram message to every chat id in TELEGRAM_CHAT (separate several with commas).
    Token and chat ids come from the server settings, never from this file."""
    token = os.environ.get("TELEGRAM_TOKEN")
    chats = [c.strip() for c in os.environ.get("TELEGRAM_CHAT", "").split(",") if c.strip()]
    if not token or not chats:
        print("Telegram is not set up (TELEGRAM_TOKEN / TELEGRAM_CHAT missing)")
        return False
    all_ok = True
    for chat in chats:
        try:
            res = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                data={"chat_id": chat, "text": text}, timeout=20).json()
        except Exception as e:
            print("Telegram failed:", type(e).__name__)  # not the full error: it could contain the token
            all_ok = False
            continue
        if not res.get("ok"):
            print(f"Telegram failed for chat {chat}:", res.get("description"))
            all_ok = False
    return all_ok


def build_report(rows, title):
    groups = NAME_FILTER if NAME_FILTER else [""]
    lines = [f"{title} {now().strftime('%H:%M')}", ""]
    tot_spend = tot_results = tot_on = tot_all = 0
    for g in groups:
        mine = [r for r in rows if g.lower() in r["name"].lower()
                and not any(o.lower() in r["name"].lower() for o in groups[:groups.index(g)] if o)]
        if not mine:
            continue
        spend = sum(r["spend"] for r in mine)
        results = sum(r["results"] for r in mine)
        tot_spend += spend
        tot_results += results
        on = sum(1 for r in mine if r["active"])
        tot_all += len(mine)
        tot_on += on
        cost = f"${spend / results:.2f} each" if results else "no cost yet"
        lines.append(f"{g.upper() or 'ALL'}: {results} messages | {cost} | ${spend:.2f} spent"
                     f" | {on} on / {len(mine) - on} off")
    cost = f"${tot_spend / tot_results:.2f} each" if tot_results else "no cost yet"
    lines += ["", f"TOTAL: {tot_results} messages | {cost} | ${tot_spend:.2f} spent"
              f" | {tot_on} on / {tot_all - tot_on} off"]
    dups = duplicated_today()
    if dups:
        lines += ["", f"DUPLICATED TODAY ({len(dups)}):"]
        for d in dups:
            lines.append(f"- {d['campaign']} ({d['why']}) at {d['time'][11:16]}"
                         + (f" -> {d['new_name']}" if d.get("new_name") else ""))
    else:
        lines += ["", "Duplicated today: none"]
    return "\n".join(lines), tot_spend


def maybe_report(rows, state, in_window, h):
    """Hourly report during working hours, plus one final report after the end of the day."""
    if not REPORT_EVERY:
        return
    ts = now().timestamp()
    if in_window and ts - state["last_report"] >= REPORT_EVERY:
        text, spend = build_report(rows, "Report")
        if spend > 0:  # nothing spent yet: stay quiet and check again next time
            telegram(text)
            state["last_report"] = ts
    elif not in_window and h >= END_HOUR and not state["final_report"]:
        text, spend = build_report(rows, "End of day report")
        if spend > 0:
            telegram(text)
        state["final_report"] = True


def cycle(state):
    rows = get_campaigns()
    if rows is None:
        return
    t = now()
    h = t.hour + t.minute / 60
    first_start = min([START_HOUR] + list(START_HOUR_BY_NAME.values()))
    in_window = first_start <= h < END_HOUR
    total = sum(r["spend"] for r in rows)

    mode = "DRY RUN (nothing is changed)" if DRY_RUN else "LIVE"
    where = "working hours" if in_window else "off hours"
    flt = ", ".join(NAME_FILTER) if NAME_FILTER else "ALL"
    print(f"\n{BOLD}{t.strftime('%H:%M:%S')} | {mode} | {where} | filter: {flt} | "
          f"{len(rows)} campaigns | spent today ${total:.2f} of ${DAILY_BUDGET:.2f}{RESET}")

    if not in_window:
        # after 19:00: turn everything off (once per day)
        if h >= END_HOUR and not state["off_done"]:
            res = [set_status(r, "PAUSED", "end of day", "OFF") for r in rows if r["active"]]
            if not DRY_RUN and all(res):
                state["off_done"] = True

    elif state["budget_hit"] or total >= DAILY_BUDGET:
        # daily budget reached: turn everything off, nothing comes back on today
        if not DRY_RUN:
            state["budget_hit"] = True
        for r in rows:
            if r["active"]:
                set_status(r, "PAUSED", f"daily budget reached ${total:.2f}", "STOP")
        if REPORT_EVERY and not state["budget_report"]:
            telegram(build_report(rows, "Daily budget reached, all campaigns paused")[0])
            state["budget_report"] = True

    else:
        # campaigns that are off while too expensive are remembered, so they can come back when the cost drops
        off_bad = [r["id"] for r in rows if not r["active"] and is_bad(r)]
        if not DRY_RUN:
            for cid in off_bad:
                if cid not in state["stopped"]:
                    state["stopped"].append(cid)
        stopped = set(state["stopped"]) | set(off_bad)

        # morning: each girl is turned on at her own start hour (once per day, skips the too expensive ones)
        if not state["on_done"]:
            groups = NAME_FILTER if NAME_FILTER else [""]
            for g in groups:
                if g in state["on_groups"] or h < start_hour_of(g):
                    continue
                res = [set_status(r, "ACTIVE", "start of day", "ON")
                       for r in rows if group_of(r["name"]) == g and not r["active"] and r["id"] not in stopped]
                if not DRY_RUN and all(res):
                    state["on_groups"].append(g)
            if not DRY_RUN and all(g in state["on_groups"] for g in groups):
                state["on_done"] = True

        # stopped campaigns whose cost dropped back under the limit: turn them on again
        for r in rows:
            if (r["active"] or r["id"] not in state["stopped"]
                    or r["cost"] is None or r["cost"] >= MAX_COST):
                continue
            if state["reenabled"].get(r["id"], 0) >= REENABLE_MAX_PER_DAY:
                continue
            if set_status(r, "ACTIVE", f"cost back to ${r['cost']:.2f}", "ON"):
                state["reenabled"][r["id"]] = state["reenabled"].get(r["id"], 0) + 1
                state["stopped"].remove(r["id"])

        # during the day: stop bad ones, raise budget of good ones, duplicate the best ones
        for r in rows:
            if not r["active"]:
                continue
            if is_bad(r):
                cost_txt = f"${r['cost']:.2f}" if r["cost"] is not None else "no results"
                if set_status(r, "PAUSED", f"cost {cost_txt}", "STOP"):
                    if r["id"] not in state["stopped"]:
                        state["stopped"].append(r["id"])
            elif r["cost"] is not None:
                if r["results"] >= BOOST_MIN_RESULTS:
                    pct = tier_value(BOOST_TIERS, r["cost"])
                    if pct:
                        try_boost(r, state, pct)
                if r["results"] >= DUPLICATE_MIN_RESULTS:
                    try_duplicate(r, state)

    maybe_report(rows, state, in_window, h)

    # table in the same order as Meta: cost per result (highest first), no-result ones at the bottom
    rows.sort(key=lambda r: (r["cost"] is None, -(r["cost"] if r["cost"] is not None else r["spend"])))
    shown = rows if SHOW_OFF else [r for r in rows if r["active"]]
    hidden = len(rows) - len(shown)
    print(f"{BOLD}{'':7}{'ID':<7}{'CAMPAIGN':38}{'SPENT':>8}{'RESULTS':>9}{'COST':>9}{'BUDGET':>9}  STATE{RESET}")
    for r in shown:
        cost_txt = f"${r['cost']:.2f}" if r["cost"] is not None else "-"
        budget_txt = f"${r['budget'] / 100:.2f}" if r["budget"] is not None else "-"
        state_txt = paint("●", GREEN if r["active"] else GREY) + (" Active" if r["active"] else " Off")
        print(paint(f"{r['mark']:7}", MARK_COLORS.get(r["mark"], ""))
              + f"{r['id'][-5:]:<7}{r['name'][:36]:38}"
              + f"{'$' + format(r['spend'], '.2f'):>8}{r['results']:>9}"
              + paint(f"{cost_txt:>9}", cost_color(r))
              + f"{budget_txt:>9}  {state_txt}")
    if hidden:
        print(paint(f"       ({hidden} stopped/off campaigns hidden - SHOW_OFF on line 22 shows them)", GREY))


if __name__ == "__main__":
    while True:
        state = load_state()
        try:
            cycle(state)
        except Exception as e:
            print("Error:", e)
        save_state(state)
        time.sleep(INTERVAL)
