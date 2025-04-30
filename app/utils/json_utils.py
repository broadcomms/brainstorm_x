# app/utils/json_utils.py
import re
import json
from flask import current_app

def extract_json_block(text: str) -> str:
    """
    Extracts the first complete JSON object or array from a string,
    handling optional markdown code fences (```json ... ```).
    Returns an empty string if no valid JSON block is found.
    """
    if not text:
        return ""

    # Pattern to find JSON within ```json ... ``` fences
    fence_pattern = r"```json\s*(\{[\s\S]*?\}|\[[\s\S]*?\])\s*```"
    fence_match = re.search(fence_pattern, text, re.IGNORECASE | re.DOTALL)

    if fence_match:
        potential_json = fence_match.group(1).strip()
        # Verify it's likely valid JSON before returning
        try:
            json.loads(potential_json)
            current_app.logger.debug("[extract_json_block] Extracted JSON from fenced block.")
            return potential_json
        except json.JSONDecodeError:
            current_app.logger.warning("[extract_json_block] Found fenced block, but content is invalid JSON. Falling back.")
            # Fall through to search outside fences if fenced content is invalid

    # If no valid fenced block, find the first '{' or '[' that starts a JSON structure
    # Use non-greedy match for the first object or array found
    first_obj_match = re.search(r"\{[\s\S]*?\}", text, re.DOTALL)
    first_arr_match = re.search(r"\[[\s\S]*?\]", text, re.DOTALL)

    first_match_text = None

    # Determine which structure appears first
    obj_start = first_obj_match.start() if first_obj_match else float('inf')
    arr_start = first_arr_match.start() if first_arr_match else float('inf')

    if obj_start < arr_start and first_obj_match:
        first_match_text = first_obj_match.group(0)
    elif arr_start < obj_start and first_arr_match:
        first_match_text = first_arr_match.group(0)
    elif first_obj_match: # Only object found
        first_match_text = first_obj_match.group(0)
    elif first_arr_match: # Only array found
        first_match_text = first_arr_match.group(0)


    if first_match_text:
        # Verify the extracted block is valid JSON
        try:
            json.loads(first_match_text)
            current_app.logger.debug("[extract_json_block] Extracted first JSON object/array found.")
            return first_match_text
        except json.JSONDecodeError as e:
            current_app.logger.warning(f"[extract_json_block] Found potential JSON, but failed validation: {e}. Content: {first_match_text[:100]}...")
            return "" # Return empty if validation fails

    current_app.logger.warning("[extract_json_block] No valid JSON object or array found in the text.")
    return "" # Return empty string if nothing found
