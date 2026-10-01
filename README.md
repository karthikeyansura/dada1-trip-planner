# DADA1: Trip Planner Agent

```
Deployed app: <your Render URL>
```

The app runs on Render's free tier and sleeps when idle, so the first request can take about 30 seconds.

## Run

```bash
cd trip_planner
pip install -r requirements.txt
python -m agent.agent   # terminal chat
python app.py           # web UI at http://127.0.0.1:7860
```

Requires `GOOGLE_API_KEY`, `TAVILY_API_KEY` and `TICKETMASTER_API_KEY`.

## Tools

- `get_weather`: Open-Meteo forecast for the city's 16-day local window; flags ambiguous city names.
- `find_attractions`, `find_restaurants`, `get_transit_info`: Tavily web search.
- `find_events`: Ticketmaster events for one day, or the next 7 days.
- `find_performer_events` (extra credit): Ticketmaster Attraction Search, then Event Search by `attractionId`.

## API notes

- Ticketmaster ignores `localEndDateTime`, so a one-day window is sent as a `localStartDateTime` range.
- Ticketmaster ignores text after a comma in `city`, so events are filtered by the geocoded coordinates.
- Most events have no `priceRanges`. A missing price is reported as unknown and `0` as a placeholder.
- An unknown category returns zero events without an error (`theater` returns none, `theatre` works).
- Some events are not linked to any attraction, so `find_performer_events` also searches by name and marks those matches as unconfirmed.
- Tavily returns an `error` field instead of raising on a bad key, so `_search` reports it as a failed search.
