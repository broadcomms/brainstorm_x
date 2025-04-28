# app/service/routes/tip.py

from flask import jsonify
from flask_login import login_required
from langchain_ibm import WatsonxLLM
from langchain_core.prompts import PromptTemplate
from app.config import Config
# Import the blueprint and the helper function from agent.py
from .agent import agent_bp, aggregate_pre_workshop_data
import markdown # If you plan to return HTML directly later

# #-----------------------------------------------------------
# # 2.d Generate tips for participants

def generate_tip_text(workshop_id):
    """Generates only the tip text using the LLM."""
    pre_workshop_data = aggregate_pre_workshop_data(workshop_id)
    if not pre_workshop_data:
        return "No pre‑workshop data found."
    
    tip_prompt_template = """
        You are an AI assistant providing helpful advice to the workshop participants.
        Based *only* on the workshop context provided below, generate ONE concise and actionable tip to help participants prepare for the workshop.
        The tip should be directly related to the workshop's objective or agenda.

        Workshop Context:
        {pre_workshop_data}

        Instructions:
        - Generate ONE tip.
        - Keep it short and brief.
        - Ensure it relates to the workshop context (based on the Title and Objective).
        
        Format:
        Output MUST be valid JSON with the key:
        - tip: The workshop tip.

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
    tip_prompt = PromptTemplate.from_template(tip_prompt_template)
    chain = tip_prompt | watsonx_llm
    raw = chain.invoke({"pre_workshop_data": pre_workshop_data})
    
    print(f"[DEBUG] Workshop raw LLM tip output: {workshop_id}: {raw}") # DEBUG CODE
    #
    # # Logic to extract the text from the raw output
    #

    # first grab the JSON block from the raw output
    m2 = re.search(r"(\{.*?\})", raw, re.DOTALL)
    json_blob = m2.group(1) if m2 else raw
    try:
        parsed = json.loads(json_blob)
        # if successful, return the icebreaker question
        return parsed.get("tip", "").strip()
    except json.JSONDecodeError:
        # if parsing fails and no json{} or {} is found return the raw LLM output
        return raw.strip()

@agent_bp.route("/generate_tips/<int:workshop_id>", methods=["POST"])
@login_required
def generate_tips(workshop_id):
    tip = generate_tip_text(workshop_id)
    return jsonify({"tip": tip}), 200