import json
from flask import current_app
from app.utils.data_aggregation import aggregate_pre_workshop_data
from app.utils.json_utils import extract_json_block
from app.models import BrainstormTask, Workshop
from app.extensions import db
from app.config import Config
from langchain_ibm import WatsonxLLM
from langchain_core.prompts import PromptTemplate


def get_brainstorming_task_payload(workshop_id: int) -> dict:
    """
    Generates the payload for a brainstorming task.
    1. Aggregates pre-workshop data.
    2. Calls the LLM to generate the brainstorming task.
    3. Parses and validates the response.
    4. Saves the task to the database.
    Returns:
      - a dict payload on success
      - (error_message, status_code) tuple on failure
    """
    current_app.logger.debug(f"[Brainstorming Service] Generating brainstorming task for workshop {workshop_id}")

    # Step 1: Aggregate pre-workshop data
    workshop_data = aggregate_pre_workshop_data(workshop_id)
    if not workshop_data:
        current_app.logger.error(f"[Brainstorming Service] Failed to aggregate data for workshop {workshop_id}")
        return "Failed to aggregate workshop data.", 500

    # Step 2: Call the LLM to generate the brainstorming task
    raw_task_data = generate_next_task_text(workshop_id, action_plan_item=None)
    current_app.logger.debug(f"[Brainstorming Service] Raw LLM response: {raw_task_data}")

    # Step 3: Extract and parse the JSON block
    json_block = extract_json_block(raw_task_data)
    if not json_block:
        current_app.logger.error(f"[Brainstorming Service] Could not extract valid JSON block for workshop {workshop_id}. Raw: {raw_task_data[:200]}")
        return "Failed to extract valid task JSON from AI response.", 500

    try:
        # Parse the extracted JSON block
        payload = json.loads(json_block)
        if not isinstance(payload, dict):
            raise ValueError("LLM did not return a valid JSON object.")

        # Validate required fields
        required_keys = ["title", "task_type", "task_description", "instructions", "task_duration"]
        if not all(key in payload for key in required_keys):
            missing = [key for key in required_keys if key not in payload]
            current_app.logger.warning(f"[Brainstorming Service] Task payload missing keys: {missing}")
            return f"Task payload missing required keys: {missing}", 500

        # Ensure duration is an integer
        try:
            payload['task_duration'] = int(payload.get('task_duration', 60))
        except (ValueError, TypeError):
            current_app.logger.warning(f"[Brainstorming Service] Invalid task_duration '{payload.get('task_duration')}', defaulting to 60.")
            payload['task_duration'] = 60

        # Step 4: Save the task to the database
        workshop = Workshop.query.get(workshop_id)
        if not workshop:
            current_app.logger.error(f"[Brainstorming Service] Workshop {workshop_id} not found.")
            return "Workshop not found.", 404

        brainstorming_task = BrainstormTask(
            workshop_id=workshop_id,
            title=payload["title"],
            prompt=json.dumps(payload),  # Save the full JSON payload
            duration=payload["task_duration"],
            status="pending"
        )
        db.session.add(brainstorming_task)
        db.session.commit()

        # Update the workshop's current task
        workshop.current_task_id = brainstorming_task.id
        workshop.current_task_index = (workshop.current_task_index or 0) + 1
        db.session.commit()

        current_app.logger.info(f"[Brainstorming Service] Successfully created brainstorming task for workshop {workshop_id}")
        return payload

    except json.JSONDecodeError as e:
        current_app.logger.error(f"[Brainstorming Service] JSON parse error for workshop {workshop_id}: {e}. Block: {json_block}")
        return f"Invalid task JSON received from AI (parse error): {e}", 500
    except Exception as e:
        current_app.logger.error(f"[Brainstorming Service] Unexpected error for workshop {workshop_id}: {e}", exc_info=True)
        return "Unexpected error processing brainstorming task.", 500
    
    

# --- MOVED FUNCTION ---
def generate_next_task_text(workshop_id, action_plan_item=None):
    """
    Generates the next brainstorming task as a JSON payload,
    optionally focusing on a specific action_plan_item.
    """
    pre_workshop_data = aggregate_pre_workshop_data(workshop_id)
    if not pre_workshop_data:
        # Return as JSON string to match expected format in routes.py
        return json.dumps({"error": "Workshop data unavailable."})

    # --- Modify Prompt Based on Action Plan Item ---
    if action_plan_item and isinstance(action_plan_item, dict):
        phase_context = f"""
            Current Action Plan Phase:
            - Phase Name: {action_plan_item.get('phase', 'N/A')}
            - Phase Description: {action_plan_item.get('description', 'N/A')}

            Based on this specific phase and the overall workshop context, create the next task.
            """
    else:
        # Default context if no specific phase is provided for brainstorming
        phase_context = "General Idea Generation"
    # ---------------------------------------------

    current_app.logger.debug(f"[Agent/Brainstorming] phase_context: {phase_context} for workshop {workshop_id}...")

    prompt_template = f"""
                        You are the facilitator for a brainstorming workshop.

                        Current Action Plan Context:
                        {phase_context}

                        Workshop Context:
                        {{pre_workshop_data}}

                        Produce output as a valid JSON object with these keys:
                        - title: A very short, engaging title for this task (related to the current phase if provided).
                        - task_type: brainstorming
                        - task_description: The specific question or prompt participants should address for this task. Make it actionable.
                        - instructions: Clear, concise instructions on how participants should contribute (e.g., "Submit your ideas individually using the input field below.").
                        - task_duration: Suggested time for this task in SECONDS (e.g., 180 for 3 minutes, 300 for 5 minutes). Be realistic based on the task.

                        Respond with *only* the valid JSON object, nothing else before or after.
                        """
    # Ensure the placeholder is correctly formatted for PromptTemplate
    prompt_template_formatted = prompt_template.replace("{pre_workshop_data}", "{pre_workshop_data}")

    watsonx = WatsonxLLM(
        model_id="ibm/granite-3-3-8b-instruct", # Or your preferred model
        url=Config.WATSONX_URL,
        project_id=Config.WATSONX_PROJECT_ID,
        apikey=Config.WATSONX_API_KEY,
        params={
            "decoding_method": "greedy", # Greedy might be better for structured JSON
            "max_new_tokens": 350,      # Allow slightly more tokens for potentially more complex tasks
            "min_new_tokens": 70,
            "temperature": 0.6,         # Slightly lower temp for more focused output
            "repetition_penalty": 1.1
        }
    )
    prompt = PromptTemplate.from_template(prompt_template_formatted)
    chain = prompt | watsonx
    raw = chain.invoke({"pre_workshop_data": pre_workshop_data})

    current_app.logger.debug(f"[Agent/Brainstorming] Raw next task for workshop {workshop_id} (Phase: {action_plan_item.get('phase', 'N/A') if action_plan_item else 'Generic'}): {raw}")

    # Return the raw output, route will handle cleaning/parsing
    return raw
# --- END MOVED FUNCTION ---
