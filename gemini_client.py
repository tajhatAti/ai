"""
Gemini কল করার centralized জায়গা — key rotation, JSON parsing, আর
rate-limit/error হলে পরের key দিয়ে retry।
"""

import json
import re
import time

import google.generativeai as genai

from config import CFG

MAX_CALL_RETRIES = 3


def strip_code_fences(text):
    text = text.strip()
    match = re.match(r"^```[a-zA-Z0-9_\-]*\n(.*)\n```$", text, flags=re.DOTALL)
    if match:
        return match.group(1)
    lines = text.split("\n")
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def parse_json_safely(text, fallback):
    try:
        return json.loads(strip_code_fences(text))
    except Exception:
        return fallback


def call_gemini(prompt, model_name=None, want_json=False):
    """একটা key দিয়ে কল করে; ব্যর্থ হলে পরের key দিয়ে আবার চেষ্টা করে (rate limit/network issue সামলাতে)।"""
    model_name = model_name or CFG.model_light
    last_error = None

    for _ in range(MAX_CALL_RETRIES):
        api_key = CFG.next_gemini_key()
        try:
            genai.configure(api_key=api_key)
            generation_config = {"response_mime_type": "application/json"} if want_json else None
            model = genai.GenerativeModel(model_name, generation_config=generation_config)
            response = model.generate_content(prompt)
            return response.text.strip()
        except Exception as e:
            last_error = e
            time.sleep(1.5)
            continue

    raise RuntimeError(f"Gemini call failed after {MAX_CALL_RETRIES} tries (rotating keys): {last_error}")


def call_gemini_json(prompt, model_name=None, fallback=None):
    raw = call_gemini(prompt, model_name=model_name, want_json=True)
    return parse_json_safely(raw, fallback if fallback is not None else {})
