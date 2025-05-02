# app/service/routes/discussion.py
import json
from datetime import datetime
from flask import current_app

from app.extensions import db
from app.models import Workshop, BrainstormTask
from app.config import Config, TASK_SEQUENCE
from app.utils.json_utils import extract_json_block
from app.utils.data_aggregation import aggregate_pre_workshop_data
from langchain_ibm import WatsonxLLM
from langchain_core.prompts import PromptTemplate


def generate_discussion_text(workshop_id: int, phase_context: str):
    """Generates discussion prompt text using LLM."""
    current_app.logger.debug(f"[Discussion] Generating text for workshop {workshop_id}")
    pre_workshop_data = aggregate_pre_workshop_data(workshop_id) # Get full context
    if not pre_workshop_data:
        return "Could not generate discussion prompt: Workshop data unavailable.", 500

    # Note: We might want to include the feasibility report in the context here
    # For simplicity now, we rely on the pre_workshop_data and phase context.

    prompt_template = """
You are the workshop facilitator initiating a discussion phase.

Workshop Context:
{pre_workshop_data}

Current Action Plan Context:
{phase_context}

Instructions:
Based on the workshop context and the current phase (likely following a results/feasibility review), craft a prompt to encourage open discussion among participants. Focus on gathering feedback, addressing questions, or defining next steps related to the previous phase's outcomes.

Produce output as a *single* valid JSON object with these keys:
- title: "Open Discussion" or similar engaging title.
- task_type: "discussion"
- task_description: The main question or topic for discussion (e.g., "Let's discuss the feasibility findings and potential next steps.").
- instructions: How participants should engage (e.g., "Use the chat window to share your thoughts, ask questions, and respond to others.").
- task_duration: Suggested time in SECONDS for discussion (e.g., 600 for 10 mins).

Respond with *only* the valid JSON object, nothing else.
"""

    watsonx_llm = WatsonxLLM(
        model_id=Config.WATSONX_MODEL_ID_1, # Use appropriate model
        url=Config.WATSONX_URL,
        project_id=Config.WATSONX_PROJECT_ID,
        apikey=Config.WATSONX_API_KEY,
        params={"decoding_method": "sample", "max_new_tokens": 300, "min_new_tokens": 50, "temperature": 0.8, "repetition_penalty": 1.0}
    )

    prompt = PromptTemplate.from_template(prompt_template)
    chain = prompt | watsonx_llm

    try:
        raw_output = chain.invoke({"pre_workshop_data": pre_workshop_data, "phase_context": phase_context})
        current_app.logger.debug(f"[Discussion] Raw LLM output for {workshop_id}: {raw_output}")
        return raw_output, 200
    except Exception as e:
        current_app.logger.error(f"[Discussion] LLM error for workshop {workshop_id}: {e}", exc_info=True)
        return f"Error generating discussion prompt: {e}", 500


def get_discussion_payload(workshop_id: int, phase_context: str):
    """Generates text, creates DB record, returns payload."""
    raw_text, code = generate_discussion_text(workshop_id, phase_context)
    if code != 200: return raw_text, code
    json_block = extract_json_block(raw_text)
    if not json_block: return "Could not extract valid JSON for discussion task.", 500
    try:
        payload = json.loads(json_block)
        if not all(k in payload for k in ["title", "task_description", "instructions", "task_duration"]): raise ValueError("Missing keys.")
        payload["task_type"] = "discussion"
        task = BrainstormTask(workshop_id=workshop_id, title=payload["title"], prompt=json.dumps(payload), duration=int(payload.get("task_duration", 600)), status="pending")
        db.session.add(task); db.session.flush(); payload['task_id'] = task.id
        current_app.logger.info(f"[Discussion] Created task {task.id} for workshop {workshop_id}")
        return payload
    except (json.JSONDecodeError, ValueError, TypeError) as e: current_app.logger.error(f"[Discussion] Payload error {workshop_id}: {e}\nJSON: {json_block}", exc_info=True); db.session.rollback(); return f"Invalid discussion task format: {e}", 500
    except Exception as e: current_app.logger.error(f"[Discussion] DB error {workshop_id}: {e}", exc_info=True); db.session.rollback(); return "Server error creating discussion task.", 500