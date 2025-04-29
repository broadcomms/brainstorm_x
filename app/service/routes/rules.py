# app/service/routes/rules.py

from flask import jsonify
from flask_login import login_required
from langchain_ibm import WatsonxLLM
from langchain_core.prompts import PromptTemplate
from app.config import Config
# Import the blueprint and the helper function from agent.py
from .agent import agent_bp
import markdown # If you plan to return HTML directly later
from app.utils.data_aggregation import aggregate_pre_workshop_data

# #-----------------------------------------------------------
# # 2.b Generate rules and guidelines
@agent_bp.route("/generate_rules_text/<int:workshop_id>", methods=["POST"])
@login_required
def generate_rules_text(workshop_id):
    """ Service Generates suggested workshop rules using the LLM."""
    pre_workshop_data = aggregate_pre_workshop_data(workshop_id)
    if not pre_workshop_data:
        # Return a meaningful message or error response
        return jsonify({"error": f"Could not generate rules: Workshop data unavailable."}), 404

    # Define the prompt template for generating rules
    rules_prompt_template =   """
                                You are a facilitator for a brainstorming workshop.
                                Based *only* on the detailed context provided below, create 3-5 clear, concise, and actionable rules or guidelines for the participants.
                                Focus on fostering collaboration, active participation, and respect, tailored to the workshop's specific objective and agenda.

                                Workshop Context:
                                {pre_workshop_data}

                                Instructions:
                                - Generate a numbered list of 3 to 5 rules in less than 80 words.
                                - Ensure rules are directly relevant to the workshop's title and objective.
                                - Output *only* the numbered list of rules, with no introductory sentence, explanation, or any other text before or after the list.

                                Generate the rules now:
                                """
    
    # initialize the watsonx summary llm       
    watsonx_llm_rules = WatsonxLLM(
            model_id="ibm/granite-3-3-8b-instruct",
            url=Config.WATSONX_URL,
            project_id=Config.WATSONX_PROJECT_ID,
            apikey=Config.WATSONX_API_KEY,
            params={
                "decoding_method": "greedy", # Use greedy for more predictable output adhering to instructions
                "max_new_tokens": 150,      # Adjusted for 3-5 concise rules
                "min_new_tokens": 20,
                "temperature": 0.5,         # Lower temperature for focus
                "repetition_penalty": 1.1   # Slightly discourage repetition
                # Removed top_k, top_p when using greedy
            }
        )
    # Define llm prompt
    rules_prompt = PromptTemplate.from_template(rules_prompt_template)
    
    # Invoke llm chain
    chain = rules_prompt | watsonx_llm_rules
    raw_rules = chain.invoke({"pre_workshop_data": pre_workshop_data})

    try:
        raw_rules = chain.invoke({"pre_workshop_data": pre_workshop_data})
        # Optional logging
        # current_app.logger.debug(f"Raw rules generated for {workshop_id}: {raw_rules[:100]}...")
        print(f"[Agent] Workshop raw rules for {workshop_id}: {raw_rules}")
        return raw_rules.strip()
    except Exception as e:
        # current_app.logger.error(f"LLM invocation failed for rules generation (workshop {workshop_id}): {e}")
        print(f"[Agent] Error generating rules for {workshop_id}: {e}")
        return "Could not generate rules due to an internal error."
    

@agent_bp.route("/generate_rules/<int:workshop_id>", methods=["POST"])
@login_required
def generate_rules(workshop_id):
    """API endpoint to generate and return rules."""
    rules_text = generate_rules_text(workshop_id)
    # Check if the helper function returned an error message
    if "Could not generate rules" in rules_text:
         # You might want a different HTTP status code depending on the error
        return jsonify({"error": rules_text}), 404
    return jsonify({"rules": rules_text}), 200


