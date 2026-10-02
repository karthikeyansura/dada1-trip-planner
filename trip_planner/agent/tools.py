"""Trip planner: Tools

Two rules, the same ones from the Husky Tech agent:
  1. Return a readable string. Never raise, never return a bare dict.
  2. When you have nothing useful, SAY SO in the return value. "No results —
     try a nearby larger city" is a sentence the model can act on.
"""

import os
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from langchain.tools import tool
from langchain_tavily import TavilySearch

# Use these URLs for their corresponding tools
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
EVENTS_URL = "https://app.ticketmaster.com/discovery/v2/events.json"
ATTRACTIONS_URL = "https://app.ticketmaster.com/discovery/v2/attractions.json"

FORECAST_DAYS = 16          # all Open-Meteo will forecast: today + 15
MAX_RESULTS = 3
MAX_EVENTS = 10
MAX_CANDIDATES = 6
EVENT_RADIUS_MILES = 25
EASTERN = ZoneInfo("America/New_York")   # "today" for the user; the server runs on UTC

HOURS_NOTE = ("\nIf any snippet above does mention opening hours, do not repeat them: they "
              "may be outdated or conflicting. Point the user to the venue's own site.")
TRANSIT_NOTE = "\nGeneral guidance only: this gives no journey times or schedules for specific trips."
FREE_NOTE = ("\nThe user wants free things: use only items a snippet above says are free. Items "
             "from a page that is not about free things are not confirmed free; leave them out.")

# Open-Meteo returns a WMO code, not words. Only the common ones are listed;
# anything else falls through to "code N", which will keep the agent honest.
WEATHER_CODES = {
    0: "clear", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog", 51: "light drizzle", 53: "drizzle",
    55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "freezing rain", 71: "light snow", 73: "snow",
    75: "heavy snow", 77: "snow grains", 80: "rain showers", 81: "rain showers",
    82: "violent rain showers", 85: "snow showers", 86: "snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with hail",
}


def _search(query: str) -> str:
    """Run one web search and format the hits. Used by find_attractions, find_restaurants and get_transit_info."""
    try:
        results = TavilySearch(max_results=MAX_RESULTS).invoke({"query": query})
    except Exception as e:
        return f"The search failed ({e}). Tell the user you could not look this up."

    # Deliberate change to the provided helper: on a bad or missing key Tavily
    # returns {"error": ...} instead of raising, which read as "No results"
    if isinstance(results, dict) and results.get("error"):
        return "The search failed (Tavily returned an error). Tell the user you could not look this up."

    hits = results.get("results", []) if isinstance(results, dict) else []
    if not hits:
        return (f"No results for '{query}'. Try a nearby larger city, or tell the "
                f"user you could not find anything — do not fill the gap from memory.")

    lines = []
    for i, h in enumerate(hits, 1):
        snippet = " ".join((h.get("content") or "").split())[:300]
        lines.append(f"{i}. {h.get('title', 'untitled')}\n   {h.get('url', '')}\n   {snippet}")
    return "\n".join(lines) + (
        "\nThese snippets are the only evidence you have. They do not include "
        "opening hours, and a price counts only if it is written above.")


def _with_note(results: str, note: str) -> str:
    """Append a caveat to search hits; failed or empty searches pass through unchanged."""
    return results + note if results.startswith(("1. ", "Free admission results:")) else results


def _geocode(city: str) -> tuple[str, float, float, str]:
    """City name -> (label, latitude, longitude, ambiguity_note). Written for you.

    Raises ValueError if nothing matches. The ambiguity note is the interesting
    part: this API ranks by population, so "Cambridge" puts England ahead of
    Massachusetts and "Portland" puts Oregon ahead of Maine.
    """
    r = requests.get(GEOCODE_URL, params={"name": city.strip(), "count": 5,
                                          "language": "en", "format": "json"},
                     timeout=15)
    r.raise_for_status()
    results = (r.json() or {}).get("results") or []
    if not results:
        raise ValueError(f"No place matches '{city}'.")

    def describe(r):
        return ", ".join(x for x in (r["name"], r.get("admin1"), r.get("country")) if x)

    top = results[0]
    label = describe(top)

    # Every US city shares its name with a hamlet somewhere; "Boston, Georgia (pop. 1,400)"
    # is noise, and an agent that hedges on every city stops being useful.
    top_pop = top.get("population") or 0
    rivals = [describe(o) for o in results[1:4]
              if o["name"].lower() == top["name"].lower()
              and (o.get("population") or 0) >= 0.10 * top_pop]
    note = (f" NOTE: '{city}' is ambiguous — could also be {'; '.join(rivals)}. "
            f"Confirm which one before planning." if rivals else "")
    return label, top["latitude"], top["longitude"], note


def _tm(url: str, params: dict, items: str) -> tuple[int, list]:
    """One Ticketmaster Discovery request -> (total matches, items on this page).

    items is the list under _embedded: "events" or "attractions". Errors are
    raised as RuntimeError with our own text: requests' messages include the
    URL, and the URL carries the API key.
    """
    for attempt in range(2):
        try:
            r = requests.get(url, params=params, timeout=15)
        except requests.RequestException as e:
            raise RuntimeError(f"network error ({type(e).__name__})") from None
        if r.status_code != 429 or attempt:
            break
        time.sleep(1)  # 5 requests/second limit; retry once

    if r.status_code == 429:
        raise RuntimeError("Ticketmaster is rate-limiting requests, try again in a few seconds")
    if r.status_code == 401:
        raise RuntimeError("Ticketmaster rejected the API key (HTTP 401)")
    if not r.ok:
        raise RuntimeError(f"Ticketmaster returned HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError("Ticketmaster returned an unreadable response") from None
    if "page" not in data:
        raise RuntimeError("Ticketmaster returned an incomplete response, try again")
    return data["page"]["totalElements"], data.get("_embedded", {}).get(items, [])


def _events(params: dict) -> tuple[int, list]:
    """Event Search -> (total matches, events on this page)."""
    return _tm(EVENTS_URL, params, "events")


def _pin(city: str) -> tuple[dict, str, str]:
    """City -> (location params, resolved place, note).

    Ticketmaster ignores anything after a comma in city, so the place is pinned
    by the same coordinates get_weather uses.
    """
    params = {"city": city.split(",")[0].strip()}
    try:
        place, lat, lon, note = _geocode(city)
        params.update(latlong=f"{lat},{lon}", radius=EVENT_RADIUS_MILES, unit="miles")
    except Exception:
        place, note = city, " NOTE: the city could not be pinned to one place, so venues may be elsewhere."
    return params, place, note


def _event_lines(events: list) -> tuple[list[str], int]:
    """Events -> (one line per event with repeat showtimes merged, listings covered)."""
    groups = {}
    for e in events:
        s = e.get("dates", {}).get("start", {})
        venue = (e.get("_embedded", {}).get("venues") or [{}])[0]
        g = groups.setdefault((e.get("name"), s.get("localDate"), venue.get("name")),
                              {"event": e, "venue": venue, "times": []})
        g["times"].append(s.get("localTime", "")[:5] or "time TBA")

    lines, shown = [], 0
    for (name, day, vname), g in list(groups.items())[:MAX_EVENTS]:
        shown += len(g["times"])
        e, v = g["event"], g["venue"]
        region = v.get("state", {}).get("stateCode") or v.get("country", {}).get("countryCode")
        where = ", ".join(x for x in (vname, v.get("city", {}).get("name"), region) if x)
        status = e.get("dates", {}).get("status", {}).get("code", "")
        flag = f" [{status.upper()}]" if status and status != "onsale" else ""
        times = ", ".join(dict.fromkeys(g["times"]))
        lines.append(f"- {day} {times} | {name}{flag} | {where} | {_price(e)} | {e.get('url', 'no link')}")
    return lines, shown


def _listing_header(total: int, shown: int) -> str:
    scope = "all shown" if shown == total else f"showing the first {shown}, so more exist"
    return f"{total} listing{'s' if total != 1 else ''} matched, {scope} (repeat showtimes merged)"


PRICE_FOOTER = "\n'price not published' means unknown, not free. Ticketmaster lists ticketed events only."


def _price(event: dict) -> str:
    """priceRanges -> readable price. Absent or 0 is never reported as free."""
    ranges = event.get("priceRanges") or []
    if not ranges:
        return "price not published"
    lo = min(r.get("min") or 0 for r in ranges)
    hi = max(r.get("max") or 0 for r in ranges)
    if not hi:
        return "price listed as 0 (seller placeholder, not free)"
    cur = ranges[0].get("currency", "USD")
    sym = "$" if cur == "USD" else f"{cur} "
    if lo == hi:
        span = f"{sym}{hi:.2f}"
    else:
        span = f"{sym}{lo:.2f}-{hi:.2f}" if lo else f"up to {sym}{hi:.2f}"
    return f"{span} (Ticketmaster)"


# NOTE: The tools already have some docstring descriptions
# but you can modify them if you find the descriptions insufficient or inaccurate.
@tool
def get_weather(city: str, date: str = "") -> str:
    """Get the weather forecast for a city on a date (YYYY-MM-DD).

    Only the next 16 days can be forecast. For any date beyond that the forecast
    does not exist, and this tool will say so.  Say that plainly instead of
    describing what the season is usually like. Leave date empty for today.

    The city name is resolved to a real place, and this tool tells you WHICH
    place it picked. If other places share the name, it says so: check it matches
    what the user meant before you plan anything. Once confirmed, qualify the
    name with a comma: "Cambridge, MA", "Portland, ME", "Cambridge, England"
    ("UK" is not recognised; use "GB" or "England").
    """
    try:
        label, lat, lon, note = _geocode(city)
        r = requests.get(FORECAST_URL, params={
            "latitude": lat, "longitude": lon, "timezone": "auto",
            "forecast_days": FORECAST_DAYS, "temperature_unit": "fahrenheit",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        }, timeout=15)
        r.raise_for_status()
        daily = r.json()["daily"]
    except Exception as e:
        return f"Could not get the weather for '{city}': {str(e).rstrip('.')}. Tell the user the forecast is unavailable."

    # Dates are local to the city, so the first one is its "today"
    days = daily["time"]
    window = f"{days[0]} to {days[-1]}"
    day = date.strip() or days[0]
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return f"'{date}' is not a YYYY-MM-DD date. Forecasts for {label} exist for {window}.{note}"

    if day not in days:
        why = "is in the past" if day < days[0] else f"is beyond the {FORECAST_DAYS}-day forecast"
        return (f"No forecast for {label} on {day}: that date {why}. Forecasts exist only for "
                f"{window}. Do not describe typical seasonal weather instead.{note}")

    i = days.index(day)
    code, hi, lo, rain = (daily[k][i] for k in (
        "weather_code", "temperature_2m_max", "temperature_2m_min", "precipitation_probability_max"))
    # The last days of the window can be returned with no values
    if None in (code, hi, lo):
        return f"No forecast data published yet for {label} on {day}. Try a date closer to today.{note}"

    sky = WEATHER_CODES.get(code, f"code {code}")
    chance = f"{rain}% chance of rain" if rain is not None else "chance of rain unknown"
    # With no date asked, give the window so later dates are not guessed
    span = "" if date.strip() else f" Forecasts exist only for {window}."
    return f"{label}, {day}: {sky}, high {hi:.0f}°F, low {lo:.0f}°F, {chance}.{span}{note}"


@tool
def find_attractions(city: str, interest: str = "") -> str:
    """Find sights, museums, parks and landmarks in a city.

    interest narrows it, e.g. "art", "history", "outdoors", "free",
    "rainy day indoor". When interest has "free" and other words, the result
    also has a "Free admission results" part. Use this for things to SEE. For food use
    find_restaurants; for things happening on a particular date use find_events.
    It does not return opening hours: never state hours, even if a snippet
    shows them.
    """
    free = re.search(r"\bfree\b", interest, re.I)
    found = _search(" ".join(f"{interest} things to see in {city}".split()))
    # A long query with "free" returns paid lists, so free admission is also searched alone
    rest = " ".join(w for w in interest.split() if w.lower() not in {"free", "rainy", "rain", "day"})
    if free and interest.strip().lower() != "free":
        extra = _search(" ".join(f"free admission {rest} attractions in {city}".split()))
        if extra.startswith("1. "):
            found = (found + "\n\n" if found.startswith("1. ") else "") + "Free admission results:\n" + extra
    return _with_note(found, HOURS_NOTE + (FREE_NOTE if free else ""))


@tool
def find_restaurants(city: str, budget: str = "") -> str:
    """Find places to eat in a city.

    budget narrows it, e.g. "cheap", "mid-range", "fine dining", and can carry
    the meal or cuisine, e.g. "cheap lunch", "vegetarian dinner".
    Use this for food and drink only. It does not return opening hours or
    current prices, so do not promise either.
    """
    return _with_note(_search(" ".join(f"best {budget} restaurants in {city}".split())), HOURS_NOTE)


@tool
def find_events(city: str, date: str = "", category: str = "") -> str:
    """Find real ticketed events in a city: concerts, sport, theatre, festivals.

    date is ONE day, YYYY-MM-DD, in the venue's own local time. For a weekend or
    any span of days, call once per day. Leave it empty for the next 7 days.
    Unlike the search tools this returns structured records: real dates, real
    venues, real links.

    category narrows by type. The five broad ones always work: "music", "sports",
    "arts & theatre", "film", "miscellaneous". Many narrower ones work too —
    "comedy", "rock", "jazz", "classical", "hip-hop", "country", "pop",
    "basketball", "hockey", "theatre", "musical", "dance", "family". Leave it
    empty for everything. Spell it "theatre": "theater" matches nothing.
    "opera" and "magic" are valid but rare, so many cities have none.

    A category the API does not recognise returns ZERO EVENTS rather than an
    error — identical to a genuinely quiet weekend. When a filtered search is
    empty this tool checks again without the filter and tells you what it found.

    city is resolved to the same place get_weather picks, and only venues near it
    are returned. Qualify shared names: "Portland, ME", "Cambridge, MA".

    MOST EVENTS COME BACK WITH NO PRICE. The API documents a price field and
    fills it in for only a minority of events. "price not published" means
    exactly that. Do not substitute a guess, and do not read a missing price as
    free admission. A price published as 0 is a placeholder, not a free ticket.

    This only covers events sold through Ticketmaster. A quiet result does not
    mean nothing is happening. Say that rather than implying the city is empty.
    For one named act, team or show, use find_performer_events instead.
    """
    key = os.getenv("TICKETMASTER_API_KEY")
    if not key:
        return "Event search is unavailable: TICKETMASTER_API_KEY is not set. Tell the user."

    if date.strip():
        try:
            start = end = datetime.strptime(date.strip(), "%Y-%m-%d").date()
        except ValueError:
            return f"'{date}' is not a YYYY-MM-DD date. Pass one day, or leave it empty for the next 7 days."
    else:
        start = datetime.now(EASTERN).date()
        end = start + timedelta(days=6)
    when = str(start) if start == end else f"{start} to {end}"

    location, place, note = _pin(city)
    # localStartDateTime takes a "start,end" range; localEndDateTime is ignored by the API
    params = {"apikey": key, "size": 50, "sort": "date,asc", **location,
              "localStartDateTime": f"{start}T00:00:00,{end}T23:59:59"}
    try:
        total, events = _events({**params, "classificationName": category} if category else params)
        if not total and category:
            unfiltered, _ = _events(params)
            if unfiltered:
                return (f"No '{category}' events in {place} for {when}, though {unfiltered} events exist "
                        f"without that filter. Either none match, or '{category}' is not a category "
                        f"Ticketmaster recognises. Do not report that nothing is on; retry with a "
                        f"broader category such as 'music', or none.{note}")
    except RuntimeError as e:
        return f"Event search failed: {e}. Tell the user you could not check events."
    except Exception as e:
        return f"Event search failed: unexpected error ({type(e).__name__}). Tell the user you could not check events."

    if not total:
        return (f"Ticketmaster lists no events in {place} for {when}. It covers ticketed events only, "
                f"so this does not mean nothing is happening.{note}")

    lines, shown = _event_lines(events)
    label = f" ({category})" if category else ""
    return (f"Ticketmaster events in {place}{label} for {when}: {_listing_header(total, shown)}.{note}\n"
            + "\n".join(lines) + PRICE_FOOTER)


def _act(attraction: dict) -> str:
    """Attraction -> 'Name (Segment, N upcoming in all cities)'."""
    seg = (attraction.get("classifications") or [{}])[0].get("segment", {}).get("name") or "type unknown"
    return f"{attraction['name']} ({seg}, {attraction.get('upcomingEvents', {}).get('_total', 0)} upcoming in all cities)"


@tool
def find_performer_events(performer: str, city: str, date: str = "") -> str:
    """Find when and where ONE named performer, team or touring show plays in a city.

    Use it when the user names an act: "Is Interpol playing in Boston?", "When
    do the Celtics play at home?", "Can I see Wicked while I'm there?". For a
    general "what's on", use find_events.

    Ticketmaster "attractions" are performers, teams and touring shows, NOT
    tourist attractions: searching "museum" finds ticketed touring exhibitions,
    not the Museum of Fine Arts. For sights, use find_attractions.

    performer is the act's name. If several acts match and exactly one of them
    has events in the city, this tool uses it and says so; tell the user which
    act it picked and what else matched. Otherwise it lists the candidates and
    picks none: ask the user which one, then call again with the name and
    "[id …]" exactly as listed, e.g. "Cinderella [id K8vZ9175ao7]".
    city works as in find_events ("Portland, ME"). date is ONE day, YYYY-MM-DD;
    when the trip dates are known, pass them, one call per day. Leave it empty
    for all upcoming dates.

    Prices follow the same rules as find_events: "price not published" is
    unknown, not free, and a 0 price is a placeholder.
    """
    key = os.getenv("TICKETMASTER_API_KEY")
    if not key:
        return "Event search is unavailable: TICKETMASTER_API_KEY is not set. Tell the user."

    window = {}
    if date.strip():
        try:
            day = datetime.strptime(date.strip(), "%Y-%m-%d").date()
        except ValueError:
            return f"'{date}' is not a YYYY-MM-DD date. Pass one day, or leave it empty for all upcoming dates."
        window = {"localStartDateTime": f"{day}T00:00:00,{day}T23:59:59"}

    # "Name [id XYZ]" picks one act from an earlier candidate list
    picked = re.search(r"\[id (\w+)\]\s*$", performer)
    name = performer[:picked.start()].strip() if picked else performer.strip()
    query = {"id": picked.group(1)} if picked else {"keyword": name, "size": 20}
    when = f"on {date.strip()}" if window else "on any upcoming date"
    resolved = ""
    try:
        matched, found = _tm(ATTRACTIONS_URL, {"apikey": key, **query}, "attractions")
        if not matched:
            return (f"No Ticketmaster performer, team or show matches '{performer.strip()}'. Do not guess "
                    f"one; check the spelling with the user, or use find_events for what's on generally.")
        live = [a for a in found if a.get("upcomingEvents", {}).get("_total")]
        if not live:
            return (f"'{name}' matched {matched} Ticketmaster names, but none has upcoming events: "
                    f"{'; '.join(a['name'] for a in found[:MAX_CANDIDATES])}.")

        location, place, note = _pin(city)
        params = {"apikey": key, "sort": "date,asc", **location, **window}
        exact = [a for a in live if a["name"].strip().lower() == name.lower()]
        if picked or len(exact) == 1 or len(live) == 1:
            act = live[0] if picked or not exact else exact[0]
            if not exact and not picked:
                resolved = f"'{name}' matched one act with upcoming events, listed as '{act['name']}'."
            total, events = _events({**params, "size": 50, "attractionId": act["id"]})
        else:
            # Acts naming the query as a whole word lead, then the busiest
            word = re.compile(rf"\b{re.escape(name)}\b", re.I)
            ranked = sorted(live, key=lambda a: (not word.search(a["name"]),
                                                 -a.get("upcomingEvents", {}).get("_total", 0)))
            # Acts sharing the exact name are always checked, wherever they rank
            top = ranked[:MAX_CANDIDATES] + [a for a in exact if a not in ranked[:MAX_CANDIDATES]]
            # One request: which of the top candidates has events in this place
            found_here, here = _events({**params, "size": 200, "attractionId": ",".join(a["id"] for a in top)})
            count = {a["id"]: sum(a["id"] in {x.get("id") for x in e.get("_embedded", {}).get("attractions", [])}
                                  for e in here) for a in top}
            playing = [a for a in top if count[a["id"]]]
            complete = found_here <= len(here)
            if len(playing) == 1 and complete:
                act = playing[0]
                rest = [_act(a) for a in ranked if a is not act]
                others = "; ".join(rest[:MAX_CANDIDATES - 1]) + (f"; and {len(rest) - MAX_CANDIDATES + 1} more"
                                                               if len(rest) >= MAX_CANDIDATES else "")
                resolved = (f"Resolved '{name}' to {_act(act)}: the only one of the {len(top)} matching "
                            f"acts checked with events in {place} {when}. Also matched: {others}.")
                events = [e for e in here if act["id"] in {x.get("id") for x in e.get("_embedded", {}).get("attractions", [])}]
                total = len(events)
            else:
                def here_count(a):
                    if complete:
                        return f"{count[a['id']]} in {place}"
                    return f"at least {count[a['id']]} in {place}" if count[a["id"]] else f"none seen in {place}"
                more = f" ({len(live) - len(top)} more not shown)" if len(live) > len(top) else ""
                scope = "" if complete else f" (only the first {len(here)} of {found_here} listings were checked)"
                return (f"'{name}' is ambiguous: several Ticketmaster acts match, and {len(playing)} of the "
                        f"{len(top)} checked have events in {place} {when}{scope}.{note}\n"
                        + "\n".join(f"- {_act(a)}, {here_count(a)} [id {a['id']}]" for a in top)
                        + more + "\nAsk the user which one they mean (you may say which seems likely). "
                        "Do not show the [id …] values to the user; keep them for your follow-up call. "
                        "Then call again with its name and [id …] exactly as listed. Do not pick one yourself.")

        # Some events are listed under the name without being linked to the act
        named_total, named = _events({**params, "size": 200, "keyword": act["name"]})
    except RuntimeError as e:
        return f"Event search failed: {e}. Tell the user you could not check events."
    except Exception as e:
        return f"Event search failed: unexpected error ({type(e).__name__}). Tell the user you could not check events."

    linked_ids = {e.get("id") for e in events}
    # Name matches count only if no act is attached; events linked to another act are left out
    unlinked = [e for e in named if e.get("id") not in linked_ids
                and not e.get("_embedded", {}).get("attractions")]
    if not total and not unlinked:
        return (f"{resolved + ' ' if resolved else ''}{_act(act)} has {act['upcomingEvents']['_total']} "
                f"upcoming events on Ticketmaster, but none in {place} {when}. It lists ticketed events only.{note}")

    dates = sorted({e.get("dates", {}).get("start", {}).get("localDate", "") for e in events + unlinked} - {""})
    span = date.strip() if window else f"upcoming dates {dates[0]} to {dates[-1]}"
    out = (resolved + "\n" if resolved else "") + f"Ticketmaster events for {_act(act)} in {place}, {span}.{note}"
    if not total:
        out = ("Answer: not confirmed. A listing under this name exists but is not linked to the act; "
               "do not say the act is playing.\n" + out)
    if total:
        lines, shown = _event_lines(events)
        out += f"\nLinked to the act: {_listing_header(total, shown)}.\n" + "\n".join(lines)
    else:
        out += f"\nNo events linked to the act {when}."
    if unlinked:
        lines, shown = _event_lines(unlinked)
        if named_total <= len(named):
            count_line = _listing_header(len(unlinked), shown)
        else:
            count_line = (f"{named_total} listings match the name; the first {len(named)} were checked and "
                          f"{len(unlinked)} of those are not linked, showing {shown}")
        out += (f"\nNOT linked to {act['name']} on Ticketmaster, matched by name only: say so, and tell the "
                f"user to confirm it is the same performer. {count_line}.\n" + "\n".join(lines))
    return out + PRICE_FOOTER


@tool
def get_transit_info(city: str) -> str:
    """Get general information about getting around a city: metro, bus, passes, fares.

    This returns written guidance, NOT timetables and NOT journey times. If the
    user asks how many minutes a specific trip takes, say you cannot look that
    up rather than estimating it.
    """
    return _with_note(_search(f"getting around {city} public transit fares passes guide"), TRANSIT_NOTE)


