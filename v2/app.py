"""AI Escape Room - Version 2 (Reliable Game Master).

Pipeline per turn:
    player text -> parser (guards + Gemini JSON / keyword rules) -> Action
               -> engine.handle() validates against room_config.json + game state
               -> narrator (Gemini) describes ONLY the validated facts -> output check -> UI

Run:  streamlit run v2/app.py
Works without an API key too (keyword parser + plain-text narration) for offline testing.
"""

import os

import streamlit as st
from dotenv import find_dotenv, load_dotenv

from engine import GameEngine
from narrator import narrate
from action_parser import llm_parse, rule_parse

load_dotenv(find_dotenv())
API_KEY = os.getenv("GEMINI_API_KEY")
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

st.set_page_config(page_title="AI Escape Room - V2", page_icon="🔐", layout="wide")

INSTRUCTIONS = """
**How to play**
- Type actions in plain English: *examine the bookshelf*, *take the key*,
  *enter 1234 on the drawer*, *use the key on the cabinet*.
- Only items in your **inventory** can be used.
- Type **hint** if you are stuck, **inventory** to see what you carry.
"""


@st.cache_resource
def get_engine() -> GameEngine:
    return GameEngine()


@st.cache_resource
def get_client():
    if not API_KEY:
        return None
    from google import genai
    return genai.Client(api_key=API_KEY, http_options=genai.types.HttpOptions(retry_options=genai.types.HttpRetryOptions(attempts=5, initial_delay=2)))


def reset_game() -> None:
    engine = get_engine()
    st.session_state.state = engine.new_state()
    st.session_state.messages = [{"role": "assistant", "content": engine.config["intro"]}]


def play_turn(text: str) -> str:
    engine, client, state = get_engine(), get_client(), st.session_state.state
    action = llm_parse(engine, client, MODEL, text) if client else rule_parse(engine, text)
    result = engine.handle(state, action)                      # Python decides
    st.session_state.last_action = {"action": action.__dict__, "result": result.__dict__}
    return narrate(client, MODEL, engine, state, text, result,  # LLM describes
                   st.session_state.messages)


# ---------- UI ----------
if "state" not in st.session_state:
    reset_game()

engine = get_engine()
state = st.session_state.state
public = engine.public_state(state)

with st.sidebar:
    st.title("🔐 AI Escape Room")
    st.caption("Version 2 - validated game state")
    st.markdown(INSTRUCTIONS)
    if st.button("🔄 Restart game", use_container_width=True):
        reset_game()
        st.rerun()

    st.divider()
    st.subheader("🎒 Inventory")
    st.write(", ".join(public["inventory"]) or "_empty_")
    st.subheader("🧩 Puzzles solved")
    st.progress(len(public["solved_puzzles"]) / 5, text=f"{len(public['solved_puzzles'])} / 5")
    st.subheader("🔎 Clues found")
    for clue in public["discovered_clues"] or ["_none yet_"]:
        st.write("- " + clue if not clue.startswith("_") else clue)
    st.subheader("🚪 Door")
    st.write("🟢 Open" if public["door"] == "open" else "🔴 Locked")
    st.caption(f"Turns: {state['turn']} · Wrong codes: {state['failed_attempts']}")
    if not API_KEY:
        st.warning("No GEMINI_API_KEY found - running in offline mode (keyword parser, plain narration).")
    with st.expander("Debug: last validated action"):
        st.json(st.session_state.get("last_action", {}))

st.title(engine.config["title"])

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

if state["escaped"]:
    st.success(f"🎉 You escaped in {state['turn']} turns! Press **Restart game** to play again.")
    st.stop()

if prompt := st.chat_input("What do you do?"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.spinner("The Game Master is thinking..."):
        reply = play_turn(prompt)
    st.session_state.messages.append({"role": "assistant", "content": reply})
    if st.session_state.state["escaped"]:
        st.balloons()
    st.rerun()
