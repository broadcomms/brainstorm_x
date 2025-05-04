# app/service/routes/brainstorming.py
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


def generate_brainstorming_text(workshop_id: int, phase_context: str):
    """Generates the brainstorming task text using LLM."""
    current_app.logger.debug(f"[Brainstorming] Generating text for workshop {workshop_id}, phase: {phase_context}")
    pre_workshop_data = aggregate_pre_workshop_data(workshop_id)
    if not pre_workshop_data:
        return "Could not generate brainstorming task: Workshop data unavailable.", 500

    prompt_template = """
You are the workshop facilitator guiding a brainstorming session.

Current Action Plan Context:
{phase_context}

Workshop Context:
{pre_workshop_data}

Based *only* on the provided context, generate the *next* brainstorming task.
Produce output as a valid JSON object with these keys:
- title: A very short, engaging title for this task (related to the current phase).
- task_type: "brainstorming"
- task_description: The specific question or prompt participants should address. Make it actionable and focused on the current phase.
- instructions: Clear, concise instructions (e.g., "Submit your ideas individually using the input field below.").
- task_duration: The time allocated for the task which is 60 seconds (1 minute).

Respond with *only* the valid JSON object, nothing else.
"""
    # Force task duration to 1 minutes for brainstorming 
    # - task_duration: Suggested time in SECONDS (e.g., 60 for 1 mins, 120 for 2 mins).
    #
    
    
    watsonx_llm = WatsonxLLM(
        model_id=Config.WATSONX_MODEL_ID_1, # Use appropriate model
        url=Config.WATSONX_URL,
        project_id=Config.WATSONX_PROJECT_ID,
        apikey=Config.WATSONX_API_KEY,
        params={"decoding_method": "greedy", "max_new_tokens": 350, "min_new_tokens": 70, "temperature": 0.6, "repetition_penalty": 1.1}
    )

    prompt = PromptTemplate.from_template(prompt_template)
    chain = prompt | watsonx_llm

    try:
        raw_output = chain.invoke({"pre_workshop_data": pre_workshop_data, "phase_context": phase_context})
        current_app.logger.debug(f"[Brainstorming] Raw LLM output for {workshop_id}: {raw_output}")
        return raw_output, 200
    except Exception as e:
        current_app.logger.error(f"[Brainstorming] LLM error for workshop {workshop_id}: {e}", exc_info=True)
        return f"Error generating brainstorming task: {e}", 500

# --- MODIFIED FUNCTION SIGNATURE ---
def get_brainstorming_task_payload(workshop_id: int, phase_context: str):
    """Generates text, creates DB record, returns payload."""
    raw_text, code = generate_brainstorming_text(workshop_id, phase_context)
    if code != 200:
        return raw_text, code

    json_block = extract_json_block(raw_text)
    if not json_block:
        return "Could not extract valid JSON for brainstorming task.", 500

    try:
        payload = json.loads(json_block)
        if not all(k in payload for k in ["title", "task_description", "instructions", "task_duration"]):
            raise ValueError("Missing required keys in brainstorming JSON payload.")
        payload["task_type"] = "brainstorming" # Ensure type is set

        # --- Create DB Record ---
        task = BrainstormTask(
            workshop_id=workshop_id,
            title=payload["title"],
            prompt=json.dumps(payload), # Store full payload
            duration=int(payload.get("task_duration", 180)), # Default 3 mins
            status="pending" # Will be set to running by the route
        )
        db.session.add(task)
        db.session.flush() # Get ID
        payload['task_id'] = task.id # Add task ID to payload
        # DO NOT COMMIT HERE - route will commit after updating workshop state
        current_app.logger.info(f"[Brainstorming] Created task {task.id} for workshop {workshop_id}")
        return payload # Return dict

    except (json.JSONDecodeError, ValueError, TypeError) as e:
        current_app.logger.error(f"[Brainstorming] Payload processing error for workshop {workshop_id}: {e}\nJSON Block: {json_block}", exc_info=True)
        db.session.rollback()
        return f"Invalid brainstorming task format: {e}", 500
    except Exception as e:
        current_app.logger.error(f"[Brainstorming] Unexpected error creating task for workshop {workshop_id}: {e}", exc_info=True)
        db.session.rollback()
        return "Server error creating brainstorming task.", 500
