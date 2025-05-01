#app/service/routes/introduction.py
import json
import re
from flask import current_app
# --- IMPORT UTILITIES ---
from app.utils.json_utils import extract_json_block
from app.utils.data_aggregation import aggregate_pre_workshop_data


def get_introduction_payload(workshop_id: int):
    """
    1) Aggregate pre-workshop data.
    2) Call the LLM to generate the introduction.
    3) Extract and parse the JSON.
    Returns:
      - a dict payload on success
      - (error_message, status_code) tuple on failure
    """
    current_app.logger.debug(f"[Introduction] Generating introduction text from LLM (agent.py) {workshop_id}")
    raw = generate_introduction_text(workshop_id)
    current_app.logger.debug(f"[Introduction] LLM raw response: {raw}")
    
    # Normalize the return signature
    if isinstance(raw, tuple):
        raw_text, code = raw
    else:
        raw_text, code = raw, 200

    if code != 200:
        return raw_text, code
    
    # Attempt to extract the JSON block from the raw text
    current_app.logger.debug(f"[Introduction] Extracting JSON block from LLM response: {raw_text}")
    json_block = extract_json_block(raw_text)

    try:
        current_app.logger.debug(f"[Introduction] Successfully extracted JSON block {json_block}")
        return json.loads(json_block) # return the JSON payload
    except Exception as e:
        current_app.logger.error(f"[Introduction] Failed to extract the JSON block: {e}")
        return f"Invalid Introduction JSON format. Error: {e}", 500
    


# -- GENERATE INTRODUCTION TEXT --
from app.utils.data_aggregation import aggregate_pre_workshop_data
from langchain_ibm import WatsonxLLM
from langchain_core.prompts import PromptTemplate
from flask import current_app
from app.config import Config

def generate_introduction_text(workshop_id):
    """
    Uses the same pre-workshop data + existing rules/agenda to craft:
     - a welcome
     - statement of objectives
     - reinforcement of rules
     - launch instructions for Task #1
    """
    current_app.logger.debug(f"[Introduction] Aggregating data for workshop {workshop_id}")
    pre_workshop_data = aggregate_pre_workshop_data(workshop_id)
    if not pre_workshop_data:
        return "Could not generate introduction: Workshop data unavailable.", 404
    current_app.logger.debug(f"[Introduction] Successfully Aggregated data: {pre_workshop_data}")
    
    # Define prompt template for generating introduction
    introduction_prompt_template = """
    You are the workshop facilitator. Based *only* on the workshop context below, craft:
     1) A warm welcome,
     2) A reminder of the goals & rules,
     3) A clear instruction for the first warm-up brainstorming question.

    Workshop Context:
    {pre_workshop_data}

    Generate output as valid JSON object with the keys:
    - welcome: A warm welcome message. (< 30 words)
    - goals: A statement of the workshop's goals.
    - rules: A reminder of the workshop rules.
    - instructions: Clear instructions for the warm-up brainstorming question to warm participants up.
    - task: The first warm-up brainstorming question.
    - task_type: The type of task is 'warm-up'.
    - task_duration: The time allocated for the task in seconds. (e.g., 60 for 1 minute).
    - task_description: A brief description of the task. (< 25 words)
    """
    
    # Instantiate the Watsonx LLM with the specified model and parameters
    watsonx_llm_introduction = WatsonxLLM(
        model_id="ibm/granite-3-3-8b-instruct",
        url=Config.WATSONX_URL,
        project_id=Config.WATSONX_PROJECT_ID,
        apikey=Config.WATSONX_API_KEY,
        params={
                "decoding_method":"greedy",
                "max_new_tokens":300,
                "min_new_tokens":50,
                "temperature":1.7,
                "top_k":40,
                "top_p":0.7
                }
    )

    # Build prompt and LLM chain
    introduction_prompt = PromptTemplate.from_template(introduction_prompt_template)
    chain = introduction_prompt | watsonx_llm_introduction

    try:
        raw_introduction = chain.invoke({"pre_workshop_data": pre_workshop_data})
        current_app.logger.debug(f"[Introduction] Workshop raw introduction for {workshop_id}: {raw_introduction}")
        return raw_introduction
    except Exception as e:
        current_app.logger.error(f"[Introduction] Error invoking LLM chain for workshop {workshop_id}: {e}")
        return f"Error generating introduction: {e}", 500