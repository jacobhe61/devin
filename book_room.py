#!/usr/bin/env python3
"""
Auto-book a Northwestern LibCal room (Mudd 2146 or Core Room B) the moment the slot opens.

Booking window: rooms open 7 days ahead at midnight Central.
Each room's default day is split across two emails (4h max, 1 booking/day each):
  mudd: 4-8pm + 8pm-midnight     core: 9am-1pm + 1-5pm

Usage:
  python3 book_room.py --netid abc1234 --room core  # wait for midnight, book date 7 days out
  python3 book_room.py --netid abc1234 --now      # skip waiting (date already open)
  python3 book_room.py --netid abc1234 --date 2026-11-20 --now --dry-run
"""
import argparse, re, sys, threading, time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

# ---------------- config ----------------
BASE = "https://northwestern.libcal.com"
FIRST, LAST = "Jacob", "He"
AFFILIATION = "Undergraduate"
EMAIL_A = "jacobhe2029@u.northwestern.edu"
EMAIL_B = "jacobhe@u.northwestern.edu"
# Each email: max 4h per booking, 1 booking per day per category.
ROOMS = {
    "mudd": dict(name="Mudd 2146", lid=925, gid=1584, eid=82373,
                 page="/spaces?lid=925&gid=1584",
                 blocks=[("16:00", "20:00", EMAIL_A), ("20:00", "24:00", EMAIL_B)]),  # 24:00 = midnight
    "core": dict(name="Core Room B", lid=1998, gid=1578, eid=6069,
                 page="/reserve/corerooms",
                 blocks=[("09:00", "13:00", EMAIL_A), ("13:00", "17:00", EMAIL_B)]),
}
LID = GID = EID = ROOM_NAME = PAGE = BLOCKS = None   # set from --room in main()
DAYS_AHEAD = 7
OPEN_TIME = "00:00"      # Central; polling starts LEAD_SECONDS before this
LEAD_SECONDS = 20
GIVE_UP_AFTER = 600      # seconds of polling before giving up
POLL_INTERVAL = 0.25
TZ = ZoneInfo("America/Chicago")
# ----------------------------------------

HDRS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": BASE,
}


def log(msg):
    print(f"[{datetime.now(TZ).strftime('%H:%M:%S.%f')[:-3]}] {msg}", flush=True)


def new_session(date):
    s = requests.Session()
    s.headers.update(HDRS)
    s.headers["Referer"] = f"{BASE}{PAGE}"
    s.get(f"{BASE}{PAGE}", timeout=10)   # cookies
    return s


def grid(s, date):
    nxt = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    r = s.post(f"{BASE}/spaces/availability/grid", timeout=10, data={
        "lid": LID, "gid": GID, "eid": EID, "seat": 0, "seatId": 0, "zone": 0,
        "start": date, "end": nxt, "pageIndex": 0, "pageSize": 18})
    r.raise_for_status()
    return [x for x in r.json().get("slots", []) if x["itemId"] == EID]


def wait_until_open(date):
    target = datetime.strptime(f"{date} {OPEN_TIME}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ) \
        - timedelta(days=DAYS_AHEAD)
    wake = target - timedelta(seconds=LEAD_SECONDS)
    now = datetime.now(TZ)
    if wake > now:
        log(f"Sleeping until {wake.strftime('%a %b %d %H:%M:%S')} Central")
        while (rem := (wake - datetime.now(TZ)).total_seconds()) > 0:
            time.sleep(min(rem, 30))

    s = new_session(date)
    log(f"Polling for {date} to open...")
    deadline = time.time() + GIVE_UP_AFTER + LEAD_SECONDS
    while time.time() < deadline:
        try:
            slots = grid(s, date)
            if slots:
                log(f"Open! {len(slots)} slots visible for {ROOM_NAME}")
                return slots
        except Exception as e:
            log(f"poll error: {e}")
        time.sleep(POLL_INTERVAL)
    sys.exit("Gave up: date never opened.")


def book_block(date, start, end, email, netid, dry_run, results):
    tag = f"{start}-{end} ({email})"
    try:
        s = new_session(date)   # separate cookie session per email = separate cart
        slots = grid(s, date)
        slot = next((x for x in slots if x["start"] == f"{date} {start}:00"), None)
        if not slot:
            raise RuntimeError("start slot not in grid")
        if slot.get("className"):
            raise RuntimeError(f"start slot unavailable ({slot['className']})")

        # 1) add start slot to cart
        r = s.post(f"{BASE}/spaces/availability/booking/add", timeout=10, data={
            "add[eid]": EID, "add[gid]": GID, "add[lid]": LID,
            "add[start]": f"{date} {start}", "add[checksum]": slot["checksum"],
            "lid": LID, "gid": GID, "start": date,
            "end": (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")})
        r.raise_for_status()
        j = r.json()
        if j.get("limitIssues"):
            raise RuntimeError(f"limit issue: {j['limitIssues']}")
        b = j["bookings"][0]

        # 2) pick end time from the allowed options
        if end == "24:00":   # midnight; on the newest bookable day LibCal ends it at 23:59
            nd = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
            want_end = next((o for o in (nd, f"{date} 23:59:00") if o in b["options"]), nd)
        else:
            want_end = f"{date} {end}:00"
        if want_end not in b["options"]:
            raise RuntimeError(f"end {end} not allowed; options: {b['options']}")
        i = b["options"].index(want_end)
        nxt = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        upd = {"update[id]": b["id"], "update[checksum]": b["optionChecksums"][i],
               "update[end]": want_end, "lid": LID, "gid": GID, "start": date, "end": nxt}
        cur = {"id": b["id"], "eid": EID, "seat_id": 0, "gid": GID, "lid": LID,
               "start": b["start"][:16], "end": b["end"][:16], "checksum": b["checksum"]}
        for k, v in cur.items():
            upd[f"bookings[0][{k}]"] = v
        r = s.post(f"{BASE}/spaces/availability/booking/add", data=upd, timeout=10)
        r.raise_for_status()
        b = r.json()["bookings"][0]
        if b["end"] != want_end:
            raise RuntimeError(f"end-time update failed: got {b['end']}")
        booking = {"id": b["id"], "eid": EID, "seat_id": 0, "gid": GID, "lid": LID,
                   "start": b["start"][:16], "end": b["end"][:16], "checksum": b["checksum"]}

        # 3) submit times -> get session id from the form
        times = {"patron": "", "patronHash": "", "method": 11,
                 "returnUrl": PAGE}
        for k, v in booking.items():
            times[f"bookings[0][{k}]"] = v
        r = s.post(f"{BASE}/ajax/space/times", data=times, timeout=10)
        r.raise_for_status()
        m = re.search(r'name="session"\s+value="(\d+)"', r.json().get("html", ""))
        if not m:
            raise RuntimeError(f"no session id in /times response: {r.text[:300]}")
        sid = m.group(1)

        # 4) final booking
        import json
        form = {"session": sid, "fname": FIRST, "lname": LAST, "email": email,
                "q13007": netid, "q11794": AFFILIATION,
                "bookings": json.dumps([booking], separators=(",", ":")),
                "returnUrl": PAGE, "pickupHolds": "", "method": 11}
        if dry_run:
            s.post(f"{BASE}/ajax/space/session/end", data={"session": sid}, timeout=10)
            results[tag] = f"DRY RUN ok (hold released) — would send {booking}"
            return
        r = s.post(f"{BASE}/ajax/space/book", files={k: (None, str(v)) for k, v in form.items()},
                   timeout=15)
        ok = r.status_code == 200 and "error" not in r.text.lower()[:200]
        results[tag] = ("BOOKED " if ok else f"FAILED ({r.status_code}) ") + r.text[:400]
    except Exception as e:
        results[tag] = f"FAILED: {e}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--netid", required=True)
    ap.add_argument("--room", choices=ROOMS, default="mudd")
    ap.add_argument("--date", help="YYYY-MM-DD (default: today + 7, Central)")
    ap.add_argument("--now", action="store_true", help="don't wait for opening time")
    ap.add_argument("--blocks", help='override, e.g. "23:00-24:00=a@u.northwestern.edu,16:00-20:00=b@..."')
    ap.add_argument("--dry-run", action="store_true", help="do everything except final submit")
    a = ap.parse_args()

    global LID, GID, EID, ROOM_NAME, PAGE, BLOCKS
    r = ROOMS[a.room]
    LID, GID, EID, ROOM_NAME, PAGE, BLOCKS = r["lid"], r["gid"], r["eid"], r["name"], r["page"], r["blocks"]
    if a.blocks:
        BLOCKS = [(x.split("=")[0].split("-")[0].strip(), x.split("=")[0].split("-")[1].strip(),
                   x.split("=")[1].strip()) for x in a.blocks.split(",") if x.strip()]
    if a.date:
        date = a.date
    else:
        base = datetime.now(TZ)
        if not a.now and base.strftime("%H:%M") > OPEN_TIME:   # e.g. launched 11:58pm
            base += timedelta(days=1)
        date = (base + timedelta(days=DAYS_AHEAD)).strftime("%Y-%m-%d")
    log(f"Target: {ROOM_NAME} on {date}, blocks {[(b[0], b[1]) for b in BLOCKS]}")

    if not a.now:
        wait_until_open(date)

    results, threads = {}, []
    for start, end, email in BLOCKS:
        t = threading.Thread(target=book_block, args=(date, start, end, email, a.netid, a.dry_run, results))
        t.start(); threads.append(t)
    for t in threads:
        t.join()
    for k, v in results.items():
        log(f"{k}: {v}")
    if any(not v.startswith(("BOOKED", "DRY RUN")) for v in results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
