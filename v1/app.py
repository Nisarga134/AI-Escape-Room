"""AI Escape Room - Version 1 (LLM-only Game Master).

Architecture: Streamlit UI -> full chat history + system prompt -> Gemini -> reply.
The LLM decides everything (what exists, what succeeds, when the player escapes).

Run:  streamlit run v1/app.py
"""

import os

import streamlit as st
from dotenv import find_dotenv, load_dotenv
from google import genai
from google.genai import types

from prompts import ESCAPE_MARKER, INSTRUCTIONS, INTRO, SYSTEM_PROMPT

load_dotenv(find_dotenv())
API_KEY = os.getenv("GEMINI_API_KEY")
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

st.set_page_config(page_title="AI Escape Room - V1", page_icon="🔐")


@st.cache_resource
def get_client() -> genai.Client:
    return genai.Client(api_key=API_KEY, http_options=genai.types.HttpOptions(retry_options=genai.types.HttpRetryOptions(attempts=5, initial_delay=2)))


def reset_game() -> None:
    st.session_state.messages = [{"role": "assistant", "content": INTRO}]
    st.session_state.escaped = False


def ask_game_master(messages: list[dict]) -> str:
    """Send the whole conversation to Gemini. The history is the game's only 'memory'."""
    contents = [
        types.Content(
            role="user" if m["role"] == "user" else "model",
            parts=[types.Part(text=m["content"])],
        )
        for m in messages
    ]
    response = get_client().models.generate_content(
        model=MODEL,
        contents=contents,
        config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT, temperature=0.8),
    )
    return response.text or "The room is silent... (empty response, try again)"


# ---------- UI ----------
if "messages" not in st.session_state:
    reset_game()

with st.sidebar:
    st.title("🔐 AI Escape Room")
    st.caption("Version 1 - LLM-only Game Master")
    st.markdown(INSTRUCTIONS)
    if st.button("🔄 Restart game", use_container_width=True):
        reset_game()
        st.rerun()

st.title("The Locked Library")

if not API_KEY:
    st.error("GEMINI_API_KEY is missing. Copy `.env.example` to `.env` and add your key.")
    st.stop()

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"].replace(ESCAPE_MARKER, ""))

if st.session_state.escaped:
    st.success("🎉 You escaped the library! Press **Restart game** to play again.")
    st.stop()

if prompt := st.chat_input("What do you do?"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("The Game Master is thinking..."):
            try:
                reply = ask_game_master(st.session_state.messages)
            except Exception as exc:  # network / quota errors
                reply = f"⚠️ Could not reach the Game Master: `{exc}`"
        st.markdown(reply.replace(ESCAPE_MARKER, ""))

    st.session_state.messages.append({"role": "assistant", "content": reply})
    # Win detection relies on the LLM emitting the marker -> a known V1 weakness.
    if ESCAPE_MARKER in reply:
        st.session_state.escaped = True
        st.balloons()
        st.rerun()
