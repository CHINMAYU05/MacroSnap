import logging
import re
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr

import httpx
import streamlit as st
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from prompt import SUMMARY_REQUEST_PROMPT, SYSTEM_PROMPT, WELCOME_MESSAGE_TEMPLATE


MODEL_NAME = "gemini-2.5-flash"
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


st.set_page_config(
    page_title="MacroSnap",
    page_icon="🥗",
    layout="centered",
    initial_sidebar_state="collapsed",
)


def get_secret(name: str) -> str:
    """Read a required Streamlit secret with a helpful setup error."""
    try:
        value = st.secrets[name]
    except (KeyError, FileNotFoundError):
        st.error(
            f"Missing `{name}`. Add it to `.streamlit/secrets.toml` before using MacroSnap."
        )
        st.stop()

    return str(value).strip()


GEMINI_API_KEY = get_secret("GEMINI_API_KEY")
GMAIL_ADDRESS = get_secret("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = get_secret("GMAIL_APP_PASSWORD")


@st.cache_resource
def get_gemini_client() -> genai.Client:
    """Build one Gemini client per Streamlit process."""
    return genai.Client(api_key=GEMINI_API_KEY)


gemini_client = get_gemini_client()


def gemini_error_message(error: genai_errors.APIError) -> str:
    """Return actionable guidance without exposing API response details."""
    if (
        error.status == "INVALID_ARGUMENT"
        and error.message
        and "api key not valid" in error.message.lower()
    ):
        return (
            "Google rejected the configured Gemini API key. Create a valid key "
            "in Google AI Studio, replace `GEMINI_API_KEY` in "
            "`.streamlit/secrets.toml`, and restart MacroSnap. Do not share "
            "your API key in chat."
        )

    if error.code == 429:
        return (
            "The Gemini API quota or rate limit has been reached. Check the "
            "Google AI Studio API quota and try again later."
        )

    logging.warning(
        "Gemini API request failed with status %s (%s).",
        error.code,
        error.status or "unknown status",
    )
    return (
        f"Gemini rejected the request ({error.code}, "
        f"{error.status or 'unknown status'}). Check your Gemini API "
        "configuration and try again."
    )


def render_message(message: dict) -> None:
    """Render either a text or image message."""
    with st.chat_message(message["role"]):
        if message["kind"] == "text":
            st.write(message["content"])
        elif message["kind"] == "image":
            st.image(message["content"], use_container_width=True)


def add_message(role: str, kind: str, content) -> None:
    """Save and immediately render a chat message."""
    message = {"role": role, "kind": kind, "content": content}
    st.session_state.messages.append(message)
    render_message(message)


def ask_gemini(parts: list) -> tuple[bool, str]:
    """Send a message to the existing Gemini conversation.

    Returns (success, text).
    """
    try:
        response = st.session_state.chat.send_message(parts)
        if response.text:
            return True, response.text
        return False, "I couldn't generate a response. Please try again."
    except genai_errors.APIError as error:
        return False, gemini_error_message(error)
    except httpx.HTTPError as error:
        logging.warning("Gemini network request failed: %s", type(error).__name__)
        return False, (
            "Couldn't connect to Gemini. Check your internet connection and "
            "try again."
        )


def generate_summary() -> tuple[bool, str]:
    """Ask Gemini for an email summary WITHOUT adding it to the chat history."""
    try:
        history = list(st.session_state.chat.get_history())
        history.append(
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=SUMMARY_REQUEST_PROMPT)],
            )
        )
        response = gemini_client.models.generate_content(
            model=MODEL_NAME,
            contents=history,
            config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
        )
        if response.text:
            return True, response.text
        return False, "Gemini returned an empty summary. Please try again."
    except genai_errors.APIError as error:
        return False, gemini_error_message(error)
    except httpx.HTTPError as error:
        logging.warning("Gemini network request failed: %s", type(error).__name__)
        return False, "Couldn't connect to Gemini. Check your connection and try again."


def clean_email_text(text: str) -> str:
    """Keep the email readable and a sensible length."""
    if not text or not text.strip():
        return "No nutrition summary available."

    text = text.strip()

    if len(text) > 6000:
        return text[:6000] + "\n\n[Summary shortened for email delivery.]"

    return text


def send_email(to_address: str, user_name: str, summary: str) -> tuple[bool, str]:
    """Send a summary through Gmail SMTP using an App Password."""
    message = MIMEText(clean_email_text(summary), "plain", "utf-8")
    message["Subject"] = f"Your MacroSnap nutrition summary, {user_name}"
    message["From"] = formataddr(("MacroSnap", GMAIL_ADDRESS))
    message["To"] = to_address

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.send_message(message)

        return True, "Email sent successfully."

    except smtplib.SMTPAuthenticationError:
        return False, (
            "Gmail authentication failed. "
            "Use a 16-character App Password, not your Gmail password."
        )

    except (smtplib.SMTPException, OSError) as error:
        return False, f"Email delivery failed: {error}"


# ---------------------------------------------------------
# Onboarding
# ---------------------------------------------------------

if "onboarded" not in st.session_state:
    st.title("🥗 MacroSnap")
    st.caption("Snap it. Track it. Email yourself the results.")
    st.markdown(
        "Estimate calories and macros from a meal photo or description, "
        "then send your conversation summary to your inbox."
    )

    with st.form("onboarding_form"):
        name = st.text_input("Your name", placeholder="Alex")
        email_address = st.text_input(
            "Email address for your summary", placeholder="you@example.com"
        )
        submitted = st.form_submit_button(
            "Let's go", type="primary", use_container_width=True
        )

    if submitted:
        clean_name = name.strip()
        clean_email = email_address.strip().lower()

        if not clean_name or not clean_email:
            st.warning("Please fill in both your name and email address.")
        elif not EMAIL_PATTERN.match(clean_email):
            st.warning("Please enter a valid email address.")
        else:
            st.session_state.name = clean_name
            st.session_state.email_address = clean_email
            st.session_state.chat = gemini_client.chats.create(
                model=MODEL_NAME,
                config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
            )
            st.session_state.messages = []
            st.session_state.onboarded = True
            st.rerun()

    st.stop()


# ---------------------------------------------------------
# Main chat interface
# ---------------------------------------------------------

if "messages" not in st.session_state:
    st.session_state.messages = []

header_col, button_col = st.columns([5, 2], vertical_alignment="center")

with header_col:
    st.title("🥗 MacroSnap")
    st.caption(
        f"Your nutrition buddy · summary → {st.session_state.email_address}"
    )

with button_col:
    # welcome message + at least one user message + one reply
    has_real_exchange = len(st.session_state.messages) > 2

    if st.button(
        "Send summary",
        disabled=not has_real_exchange,
        use_container_width=True,
        type="primary",
        help="Ask at least one nutrition question first.",
    ):
        with st.spinner("Preparing your email summary..."):
            ok, summary = generate_summary()

        if not ok:
            st.error(summary)
        else:
            success, info = send_email(
                st.session_state.email_address,
                st.session_state.name,
                summary,
            )
            if success:
                st.success(f"{info} Check {st.session_state.email_address}.")
            else:
                st.error(info)


# Display welcome message or previous chat history
if not st.session_state.messages:
    add_message(
        "assistant",
        "text",
        WELCOME_MESSAGE_TEMPLATE.format(name=st.session_state.name),
    )
else:
    for message in st.session_state.messages:
        render_message(message)


# ---------------------------------------------------------
# Text and image input
# ---------------------------------------------------------

user_input = st.chat_input(
    "Ask about a meal, or attach a photo",
    accept_file=True,
    file_type=["jpg", "jpeg", "png", "webp"],
)

if user_input:
    photo = user_input.files[0] if user_input.files else None
    text = user_input.text.strip() if user_input.text else ""

    parts = []

    if photo is not None:
        photo_bytes = photo.getvalue()
        add_message("user", "image", photo_bytes)
        parts.append(
            types.Part.from_bytes(data=photo_bytes, mime_type=photo.type)
        )

    if text:
        add_message("user", "text", text)
        parts.append(text)
    elif photo is not None:
        parts.append(
            "What is this meal? Estimate its calories and "
            "protein, carbs, and fat."
        )

    if parts:
        with st.spinner("Crunching the numbers..."):
            _, answer = ask_gemini(parts)

        add_message("assistant", "text", answer)
        st.rerun()


# ---------------------------------------------------------
# Disclaimer
# ---------------------------------------------------------

with st.expander("About MacroSnap"):
    st.write(
        "MacroSnap provides rough nutrition estimates for educational "
        "purposes. It is not medical advice, and estimates can vary "
        "with portion size and preparation."
    )