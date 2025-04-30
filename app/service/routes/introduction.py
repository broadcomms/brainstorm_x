import json
import re
from flask import current_app
from app.utils.data_aggregation import aggregate_pre_workshop_data
from app.service.routes.agent import generate_introduction_text

def extract_json_block(text: str) -> str:
    """
    Extract a JSON array block from the raw LLM response.
    """
    if not text:
        return text

    # 1) Prefer an explicit ```json fenced block
    fence = re.search(r"```json\s*([\s\S]*?)```", text, re.IGNORECASE)
    if fence:
        return fence.group(1).strip()

    # 2) Otherwise take the first JSON object
    obj = re.search(r"\{[\s\S]*\}", text)
    if obj:
        return obj.group(0).strip()

    # 3) Or the first JSON array
    arr = re.search(r"$begin:math:display$[\\s\\S]*$end:math:display$", text)
    if arr:
        return arr.group(0).strip()

    # 4) Give up – maybe it’s already raw JSON
    return text.strip()







def get_introduction_payload(workshop_id: int):
    """
    1) Aggregate pre-workshop data.
    2) Call the LLM to generate the introduction.
    3) Extract and parse the JSON.
    Returns:
      - a dict payload on success
      - (error_message, status_code) tuple on failure
    """
    current_app.logger.debug(f"[Introduction] Aggregating data for workshop {workshop_id}")
    pre_data = aggregate_pre_workshop_data(workshop_id)

    raw = generate_introduction_text(workshop_id)
    # normalize return signature
    if isinstance(raw, tuple):
        raw_text, code = raw
    else:
        raw_text, code = raw, 200

    if code != 200:
        return raw_text, code

    current_app.logger.debug(f"[Introduction] Raw LLM intro: {raw_text}")
    json_block = extract_json_block(raw_text)
    try:
        return json.loads(json_block)
    except Exception as e:
        current_app.logger.error(f"[Introduction] JSON parse error: {e}")
        return f"Invalid introduction JSON: {e}", 500