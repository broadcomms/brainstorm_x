# app/service/routes/icebreaker.py

from flask import jsonify
from flask_login import login_required
from langchain_ibm import WatsonxLLM
from langchain_core.prompts import PromptTemplate
from app.config import Config
# Import the blueprint and the helper function from agent.py
from .agent import agent_bp, aggregate_pre_workshop_data
import markdown # If you plan to return HTML directly later

# #-----------------------------------------------------------
# # 2.c Generate icebreaker activities

def generate_icebreaker_text(workshop_id):
    """Generates only the icebreaker text using the LLM."""
    pre_workshop_data = aggregate_pre_workshop_data(workshop_id)
    if not pre_workshop_data:
        return "Could not generate icebreaker: Workshop data unavailable."
    icebreaker_prompt_template = """
    You are a workshop assistant. Your task is to create a fun and engaging icebreaker question for the workshop.
    Based on the workshop context provided below, generate a fun, engaging, and very short icebreaker question (under 25 words).
    The icebreaker should be relevant to the workshop's title or objective.

    Workshop Context:
    {pre_workshop_data}

    Instructions:
    - Generate ONE icebreaker question.
    - Keep it short and brief under 25 words.
    - Ensure it relates to the workshop context (based on the Title and Objective).
    
    Format:
    Output MUST be valid JSON with the keys:
    - icebreaker: The icebreaker question.

    Response:
    """
    watsonx_llm = WatsonxLLM(
        model_id="ibm/granite-3-3-8b-instruct",
        url=Config.WATSONX_URL,
        project_id=Config.WATSONX_PROJECT_ID,
        apikey=Config.WATSONX_API_KEY,
        params={
            "decoding_method": "greedy", # Greedy for concise, focused tip
            "max_new_tokens": 200,       # Adjusted for 1-2 sentences
            "min_new_tokens": 5,
            "temperature": 0.9,         # Lower temperature for focus
            "repetition_penalty": 1
            # Removed top_k, top_p for greedy
        }
    )
    icebreaker_prompt = PromptTemplate.from_template(icebreaker_prompt_template)
    chain = icebreaker_prompt | watsonx_llm
    raw = chain.invoke({"pre_workshop_data": pre_workshop_data})
    
    
    
    
    print(f"[DEBUG] Workshop raw LLM icebreaker output: {workshop_id}: {raw}") # DEBUG CODE
    #
    # # Logic to extract the icebreaker question from the raw output
    #

    # first grab the JSON block from the raw output
    m2 = re.search(r"(\{.*?\})", raw, re.DOTALL)
    json_blob = m2.group(1) if m2 else raw
    try:
        parsed = json.loads(json_blob)
        # if successful, return the icebreaker question
        return parsed.get("icebreaker", "").strip()
    except json.JSONDecodeError:
        # if parsing fails and no json{} or {} is found return the raw LLM output
        return raw.strip()

@agent_bp.route("/generate_icebreaker/<int:workshop_id>", methods=["POST"])
@login_required
def generate_icebreaker(workshop_id):
    """API endpoint to generate and return an icebreaker."""
    icebreaker_text = generate_icebreaker_text(workshop_id)
    if "Could not generate icebreaker" in icebreaker_text:
        return jsonify({"error": icebreaker_text}), 404
    return jsonify({"icebreaker": icebreaker_text}), 200
