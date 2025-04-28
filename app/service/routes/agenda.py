# app/service/routes/agenda.py

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
    # --- Get pre workshop data (aggregated) ---
    pre_workshop_data = aggregate_pre_workshop_data(workshop_id)
    if not pre_workshop_data:
        # TODO: Consider logging this error
        # current_app.logger.error(f"Failed to get pre-workshop data for {workshop_id}")
        return "Could not generate agenda: Workshop data unavailable."

    # Define the prompt template for generating an agenda
    agenda_prompt_template = """
                            You are an expert workshop facilitator AI.
                            Based *only* on the detailed workshop context provided below, create a structured, timed agenda proposal.
                            The agenda should logically flow towards the workshop's objective and fit within the specified duration.

                            Workshop Context:
                            {pre_workshop_data}

                            Instructions:
                            - Analyze the Workshop Title, Objective, Duration, and Participant count/roles.
                            - Create a bulleted or numbered list representing the agenda flow.
                            - Include estimated timings for each major section (e.g., Introduction: 10 mins, Brainstorming Session 1: 30 mins, Wrap-up: 15 mins). Ensure total time roughly matches the workshop duration.
                            - Keep descriptions concise.
                            - Output *only* the agenda list itself, with no introductory sentence, explanation, confidence scores, or any other text before or after the list. Use Markdown for formatting (e.g., bullet points).

                            Generate the agenda proposal now:
                            """

    # Initialize the Watsonx LLM (adjust parameters if needed for longer/structured output)
    watsonx_llm_agenda = WatsonxLLM(
            model_id=Config.GRANITE_8B_INSTRUCT, # Using a constant from Config
            url=Config.WATSONX_URL,
            project_id=Config.WATSONX_PROJECT_ID,
            apikey=Config.WATSONX_API_KEY,
            params={
                "decoding_method": "sample", # Sample might be better for creative agenda structure
                "max_new_tokens": 350,      # Increased slightly
                "min_new_tokens": 50,
                "temperature": 0.7,
                "top_k": 50,
                "top_p": 0.9,
                "repetition_penalty": 1.05
            }
        )

    # Define llm prompt
    agenda_prompt = PromptTemplate.from_template(agenda_prompt_template)

    # Invoke llm chain
    chain = agenda_prompt | watsonx_llm_agenda
    try:
        raw_agenda = chain.invoke({"pre_workshop_data": pre_workshop_data})
        # Optional: Add basic logging
        # current_app.logger.debug(f"Raw agenda generated for {workshop_id}: {raw_agenda[:100]}...")
        print(f"[Agent] Workshop raw agenda for {workshop_id}: {raw_agenda}") # Keep if useful
        return raw_agenda.strip() # Basic cleanup
    except Exception as e:
        # Log the error
        # current_app.logger.error(f"LLM invocation failed for agenda generation (workshop {workshop_id}): {e}")
        print(f"[Agent] Error generating agenda for {workshop_id}: {e}")
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
