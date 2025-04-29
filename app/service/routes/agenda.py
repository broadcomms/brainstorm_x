# app/service/routes/agenda.py
import re  # Add this import for regex
import json
from flask import jsonify
from flask_login import login_required
from langchain_ibm import WatsonxLLM
from langchain_core.prompts import PromptTemplate
from app.config import Config
# Import the blueprint and the helper function from agent.py
from .agent import agent_bp, aggregate_pre_workshop_data
import markdown # If you plan to return HTML directly later

# -----------------------------------------------------------
# 1.b Generate workshop agenda (New Function)
def generate_agenda_text(workshop_id):
    """Generates a suggested workshop agenda using the LLM."""
    pre_workshop_data = aggregate_pre_workshop_data(workshop_id)
    if not pre_workshop_data:
        return "Could not generate agenda: Workshop data unavailable."

    agenda_prompt_template = """
                            You are an AI assistant facilitating a brainstorming workshop.
                            Based *only* on the workshop context provided below, generate a structured timed agenda for the workshop.
                            The agenda should logically flow towards achieving the workshop's objective within the workshop duration.

                            Workshop Context:
                            {pre_workshop_data}

                            Instructions:
                            - Generate 4-5 bullet points to list the agenda items.
                            - Include estimated time to complete each item.
                            - Ensure it is related to workshop context (based on the Title and Objective)
                            
                            Format:
                            Output MUST be valid JSON with the key "agenda", an array of objects each containing:
                            - "time_slot"
                            - "activity"
                            - "description"
                            - "estimated_duration"

                            Response:
                            """

    watsonx_llm_agenda = WatsonxLLM(
        model_id=Config.GRANITE_8B_INSTRUCT,
        url=Config.WATSONX_URL,
        project_id=Config.WATSONX_PROJECT_ID,
        apikey=Config.WATSONX_API_KEY,
        params={
            "decoding_method": "sample",
            "max_new_tokens": 800,
            "min_new_tokens": 50,
            "temperature": 0.7,
            "top_k": 50,
            "top_p": 0.9,
            "repetition_penalty": 1.05
        }
    )

    agenda_prompt = PromptTemplate.from_template(agenda_prompt_template)
    chain = agenda_prompt | watsonx_llm_agenda

    try:
        raw = chain.invoke({"pre_workshop_data": pre_workshop_data})
        print(f"[Agenda Service] Workshop raw agenda _ID:{workshop_id}: {raw}")  # Debugging

        # Extract JSON block using regex
        match = re.search(r"(\{.*\})", raw, re.DOTALL)
        if match:
            json_block = match.group(1)
            return json_block.strip()
        else:
            raise ValueError("No valid JSON block found in the response.")

    except Exception as e:
        print(f"[Agenda Service] Error generating agenda _ID:{workshop_id}: {e}")
        return "Could not generate agenda due to an internal error."


# API endpoint if you want a direct route to generate *only* the agenda for frontend processing.
# Note: Your current setup calls /workshop/.../regenerate/agenda which then calls generate_agenda_text
@agent_bp.route("/generate_agenda/<int:workshop_id>", methods=["POST"])
@login_required
def generate_agenda(workshop_id):
    """API endpoint to generate and return an agenda (optional direct route)."""
    agenda_text = generate_agenda_text(workshop_id)
    if "Could not generate agenda" in agenda_text:
        return jsonify({"error": agenda_text}), 500 # Use 500 for server-side generation issues
    # Return raw text or rendered HTML
    # agenda_html = markdown.markdown(agenda_text)
    return jsonify({"agenda": agenda_text}), 200 # Returning raw text for now
