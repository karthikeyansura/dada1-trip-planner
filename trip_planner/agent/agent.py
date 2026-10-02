"""Trip planner agent

    python -m agent.agent
"""

import logging
import os

from dotenv import load_dotenv

load_dotenv()

# silence a noisy advisory warning from the Google SDK
logging.getLogger("google_genai.models").setLevel(logging.ERROR)

# Fail fast and legibly: a placeholder left over from example.env otherwise
# surfaces as a 400 INVALID_ARGUMENT deep inside the first model call.
for _name in ("GOOGLE_API_KEY", "TAVILY_API_KEY", "TICKETMASTER_API_KEY"):
    if os.getenv(_name, "").startswith("your_"):
        raise SystemExit(
            f"{_name} in your .env is still the placeholder from example.env. "
            "Replace it with your real key, in .env at your project root."
        )

import re
from datetime import datetime

from langchain.agents import create_agent
from langchain.agents.middleware import ModelRequest, dynamic_prompt, wrap_model_call
from langchain.messages import HumanMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.checkpoint.memory import InMemorySaver

from .prompts import SYSTEM_PROMPT
from .tools import (EASTERN, find_attractions, find_events, find_performer_events,
                    find_restaurants, get_transit_info, get_weather)

TOOLS = [get_weather, find_attractions, find_restaurants, find_events,
         find_performer_events, get_transit_info]

# You must use an API model for online deployment. Otherwise, we cannot test it properly.
# Options: Gemini, OpenRouter, NVIDIA as posted on Piazza
model = ChatGoogleGenerativeAI(model="gemini-3.5-flash-lite")

# Local alternative for testing during development
# from langchain_ollama import ChatOllama
# model = ChatOllama(model="qwen3.5:4b", reasoning=False)

@dynamic_prompt
def dated_prompt(request: ModelRequest) -> str:
    """System prompt with today's date, rebuilt on every model call."""
    today = datetime.now(EASTERN).strftime("%A, %Y-%m-%d")
    return f"{SYSTEM_PROMPT}\n\nToday is {today} (US Eastern)."


@wrap_model_call
def retry_empty_reply(request, handler):
    """Gemini occasionally ends a turn with no text and no tool call; ask once more."""
    response = handler(request)
    reply = response.result[0]
    if not reply.text.strip() and not getattr(reply, "tool_calls", None):
        response = handler(request)
    return response


URL = re.compile(r"https?://[^\s<>\"'\]]+")


def unsourced_urls(text: str, messages: list) -> list[str]:
    """URLs in text that appear in no tool message, ignoring trailing punctuation and brackets."""
    sources = "\n".join(m.text for m in messages if m.type == "tool")
    return [u for u in (raw.rstrip(".,;:!?)*_") for raw in URL.findall(text)) if u not in sources]


@wrap_model_call
def retry_unsourced_urls(request, handler):
    """A final reply citing a URL no tool returned is regenerated once."""
    response = handler(request)
    reply = response.result[0]
    if not getattr(reply, "tool_calls", None) and unsourced_urls(reply.text, request.messages):
        response = handler(request)
    return response


trip_agent = create_agent(
    model=model,
    tools=TOOLS,
    middleware=[dated_prompt, retry_empty_reply, retry_unsourced_urls],
    checkpointer=InMemorySaver(),
)


def ask(text: str, thread: str) -> dict:
    """One turn of the conversation. The same thread keeps the memory."""
    config = {"configurable": {"thread_id": thread}}
    return trip_agent.invoke({"messages": [HumanMessage(content=text)]}, config)

if __name__ == "__main__":
    from .itinerary import build_itinerary

    print("Trip planner. Describe your trip, refine it, then type 'quit'.")
    result = None
    while True:
        user = input("You: ")
        if user.lower() in {"quit", "exit"}:
            break
        result = ask(user, "cli-demo")
        print("Agent:", result["messages"][-1].text)

    if result is not None:
        print("\n--- itinerary ---")
        print(build_itinerary(model, result).model_dump_json(indent=2))
