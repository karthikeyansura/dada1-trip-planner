"""Trip planner: the structured itinerary.

To be used to extract one typed itinerary from the conversation.

Please note `cost` is a STRING, not a number, so that
"unknown" is something the itinerary can actually say, and so a price can carry
where it came from. If it were a float the model would have to invent a figure
to fill it, and a made-up price looks exactly like a real one.
"""

from typing import Literal

from pydantic import BaseModel, Field


class Activity(BaseModel):
    """One thing the traveller does."""
    time: str = Field(description="When it starts, e.g. '09:30' or 'morning'")
    name: str = Field(description="What it is, e.g. 'Museum of Fine Arts'")
    kind: Literal["sight", "food", "event", "travel", "other"] = Field(
        description="Which tool this came from: sight, food, event, travel, other")
    cost: str = Field(
        description="'free' only when something actually says it is free; a price "
                    "a tool gave you, marked with where it came from, like "
                    "'$45-120 (Ticketmaster)' or '$27 (blog, unconfirmed)'; or "
                    "'unknown'. Most things will be 'unknown' — that is the honest "
                    "answer, and an unknown price is never 0.")
    source: str = Field(description="The URL this came from, or 'none' if there isn't one")


class DayPlan(BaseModel):
    """One day of the trip."""
    date: str = Field(description="The day as YYYY-MM-DD, or 'unknown' if the conversation never fixed one")
    weather: str = Field(
        description="What get_weather reported for this day, e.g. 'overcast, high 71°F, "
                    "9% chance of rain (Open-Meteo)', or 'no forecast available'")
    activities: list[Activity] = Field(description="The activities planned for this day, in order")


class Itinerary(BaseModel):
    """A complete plan for a trip."""
    city: str = Field(description="The city as the tools resolved it, e.g. 'Boston, Massachusetts, United States'")
    days: list[DayPlan] = Field(description="One entry per planned day, in date order")
    unknowns: list[str] = Field(
        description="Everything the tools could not confirm, one item each: opening hours "
                    "for each venue in the plan, prices that were not published, journey "
                    "times, missing forecasts")


ITINERARIES = []

ROLES = {"human": "User", "ai": "Assistant", "tool": "Tool"}

EXTRACT_PROMPT = """Extract the itinerary from this trip-planning conversation.

Use the MOST RECENT plan the assistant gave. A plan for a new city or trip replaces
earlier ones, and a refined plan replaces the version it changed.
Use only what the transcript states:
- activities: the picks in that plan, in order. Leave out alternatives.
- name: the place, or the event title for events.
- cost: a price written in a Tool message for that item, with where it came from:
  '$45-120 (Ticketmaster)', or '$10-20 (travellemming.com, unconfirmed)' for a web snippet.
  'free' only if a Tool message says that item is free, e.g. it offers free admission.
  Otherwise 'unknown'. Never 0.
- source: the URL given for that item; 'none' if there is no URL.
- unknowns: one entry per item, e.g. 'Opening hours for Boston Public Library',
  'Price for Porter Exchange'. Cover every opening hour and price in the plan that the
  tools did not confirm, plus any journey time or forecast the plan lacks.

Transcript:

"""


def build_itinerary(model, result) -> Itinerary:
    """Extract a structured itinerary from a finished planning conversation.

    This is `file_ticket` from W03b.1 pointed at a trip. Build the transcript from
    result["messages"], call model.with_structured_output(Itinerary) on it, append
    to ITINERARIES, and return it.
    """
    # Tool messages carry the URLs and prices; .text flattens Gemini's content blocks
    transcript = "\n\n".join(
        f"{ROLES.get(m.type, m.type)}{f' ({m.name})' if m.type == 'tool' else ''}: {m.text}"
        for m in result["messages"] if m.text)
    itinerary = model.with_structured_output(Itinerary).invoke(EXTRACT_PROMPT + transcript)
    ITINERARIES.append(itinerary)
    return itinerary
