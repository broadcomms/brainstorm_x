# app/agent/routes.py
# ----------------------------------------------------------





# -----------------------------------------------------------
#                   AI AGENT ROUTES
# -----------------------------------------------------------
#
# # This file contains the routes for the ai agent module.
# # Granite 3.3 Models
# 
#
# #-----------------------------------------------------------
# # Imports and blueprint setup
# #-----------------------------------------------------------
import os, json, re
from flask import Blueprint, current_app, request, jsonify
from datetime import datetime, timedelta
from flask_login import login_required
from flask_socketio import emit
from app.extensions import db
from app.models import Workshop, WorkshopParticipant, WorkshopDocument
from app.config import Config
# Import Watsonx LLM wrapper and prompt template
from langchain_ibm import WatsonxLLM, ChatWatsonx
from langchain_core.prompts import PromptTemplate
from concurrent.futures import ThreadPoolExecutor
agent_bp = Blueprint(   "agent_bp", 
                        __name__, 
                        template_folder="templates", 
                        static_folder="static",
                        url_prefix="/agent"
                    )
# #-----------------------------------------------------------








### #---------------------------------------------------------
### 1. PRE-WORKSHOP # #---------------------------------------
# # This is phase where the agent prepares for the workshop.
### #---------------------------------------------------------

# #-----------------------------------------------------------
# # 1.a Aggregate pre-workshop data
def aggregate_pre_workshop_data(workshop_id):
    """
    Aggregates comprehensive data about a workshop, its participants,
    workspace, and linked documents into a structured string format
    suitable for an LLM prompt.
    """
    print(f"[Agent] Aggregating pre-workshop data for workshop_id: {workshop_id}")

    # 1. Get the Workshop object
    workshop = Workshop.query.options(
        db.selectinload(Workshop.workspace), # Eager load workspace
        db.selectinload(Workshop.creator),   # Eager load creator
        db.selectinload(Workshop.participants).selectinload(WorkshopParticipant.user), # Eager load participants and their users
        db.selectinload(Workshop.linked_documents).selectinload(WorkshopDocument.document) # Eager load linked docs and the actual documents
    ).get(workshop_id)

    if not workshop:
        print(f"[Agent] Workshop with ID {workshop_id} not found.")
        return None # Or raise an error, depending on desired behavior

    # --- Start building the structured string ---
    data_string = f"--- Workshop Context for ID: {workshop_id} ---\n\n"

    # 2. Workshop Details
    data_string += "**Workshop Details:**\n"
    data_string += f"*   **Title:** {workshop.title}\n"
    data_string += f"*   **Objective:** {workshop.objective or 'Not specified'}\n"
    data_string += f"*   **Scheduled Date & Time:** {workshop.date_time.strftime('%Y-%m-%d %H:%M:%S UTC') if workshop.date_time else 'Not set'}\n"
    data_string += f"*   **Duration:** {f'{workshop.duration} minutes' if workshop.duration else 'Not specified'}\n"
    data_string += f"*   **Status:** {workshop.status}\n"
    
    agenda = workshop.agenda or 'No agenda provided'
    indented = agenda.replace('\n', '\n    ')
    data_string += "*   **Agenda:**\n    " + indented + "\n"
    
    creator_name = workshop.creator.first_name or workshop.creator.email
    data_string += f"*   **Created By:** {creator_name} (ID: {workshop.created_by_id})\n"

    # Find the organizer (using the helper property is cleaner if available and reliable)
    organizer = workshop.organizer # Using the @property from Workshop model
    organizer_name = organizer.first_name or organizer.email if organizer else "Not assigned"
    data_string += f"*   **Organizer:** {organizer_name}\n\n"

    # --- ADDED: Include Generated AI Content ---
    data_string += "**Generated Content (if available):**\n"
    if workshop.generated_rules:
        indented_rules = workshop.generated_rules.replace('\n', '\n    ')
        data_string += f"*   **Rules/Guidelines:**\n    {indented_rules}\n"
    else:
        data_string += "*   **Rules/Guidelines:** Not generated yet.\n"

    if workshop.generated_icebreaker:
        data_string += f"*   **Icebreaker:** {workshop.generated_icebreaker}\n"
    else:
        data_string += "*   **Icebreaker:** Not generated yet.\n"

    if workshop.generated_tip:
        data_string += f"*   **Preparation Tip:** {workshop.generated_tip}\n"
    else:
        data_string += "*   **Preparation Tip:** Not generated yet.\n"
        # --- ADDED: Include Generated Action Plan ---
    if workshop.generated_action_plan:
        indented_plan = workshop.generated_action_plan.replace('\n', '\n    ')
        #print(f"[Agent] Workshop action plan: {indented_plan}") # DEBUG CODE
        # data_string += f"*   **Action Plan:**\n    {indented_plan}\n"
        data = json.loads(indented_plan)
        markdown_output = "# Workshop Phases\n\n"
        for item in data:
            markdown_output += f"## {item['phase']}\n{item['description']}\n\n"

        print(markdown_output)
    else:
        data_string += "*   **Action Plan:** Not generated yet.\n"
    data_string += "\n"
    # --- END ADDED SECTION ---

    # 3. Workspace Details
    if workshop.workspace:
        data_string += "**Workspace Details:**\n"
        data_string += f"*   **Name:** {workshop.workspace.name}\n"
        data_string += f"*   **Description:** {workshop.workspace.description or 'No description'}\n\n"
    else:
        data_string += "**Workspace Details:**\n*   Workshop is not associated with a workspace.\n\n"


    # 4. Participant List
    # Ensure participants are loaded correctly (selectinload should handle this)
    participants = workshop.participants.all() if hasattr(workshop.participants, 'all') else list(workshop.participants)
    data_string += f"**Participants ({len(participants)}):**\n"
    if not participants:
        data_string += "*   No participants found.\n"
    else:
        # Sort participants perhaps by role then name
        participants.sort(key=lambda p: (p.role != 'organizer', (p.user.first_name or p.user.email).lower()))
        for participant in participants:
            user = participant.user
            user_name = user.first_name or user.email
            job_title = f" - Job: {user.job_title}" if user.job_title else ""
            organization = f" - Org: {user.organization}" if user.organization else ""
            data_string += f"*   {user_name} (ID: {user.user_id}) - Role: {participant.role}, Status: {participant.status}{job_title}{organization}\n"
    data_string += "\n"


    # 5. Linked Documents
    # Ensure linked_docs are loaded correctly
    linked_docs = workshop.linked_documents.all() if hasattr(workshop.linked_documents, 'all') else list(workshop.linked_documents)
    data_string += f"**Linked Documents ({len(linked_docs)}):**\n"
    if not linked_docs:
        data_string += "*   No documents linked to this workshop.\n"
    else:
        for link in linked_docs:
            doc = link.document
            # Check if doc is loaded, handle potential None if relationship fails
            if doc:
                data_string += f"*   **{doc.title}** (ID: {doc.id}): {doc.description or 'No description'}\n"
            else:
                 data_string += f"*   Linked Document (ID: {link.document_id}) - Error loading details.\n" # Handle missing doc object
        # Important Note for the LLM about document content:
        data_string += "*   *(Note: Document content analysis is not performed. Information is based on titles and descriptions.)*\n"
    data_string += "\n"

    data_string += "--- End of Workshop Context ---\n"

    print(f"[Agent] Successfully aggregated data for workshop {workshop_id}.") # DEBUG CODE
    return data_string

# #-----------------------------------------------------------
# # 1.b Generate workshop agenda







### #---------------------------------------------------------
### 2. LOBBY # #---------------------------------------------
# # This is the area where participants wait for the workshop.
### #---------------------------------------------------------


# #-----------------------------------------------------------
# # 2.b Generate rules and guidelines
@agent_bp.route("/generate_rules_text/<int:workshop_id>", methods=["POST"])
@login_required
def generate_rules_text(workshop_id):
    pre_workshop_data = aggregate_pre_workshop_data(workshop_id)
    if not pre_workshop_data:
        # Return a meaningful message or error response
        return jsonify({"error": f"Could not aggregate data for workshop {workshop_id}. It might not exist or have no participants."}), 404

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
    
    print(f"[Agent] Workshop raw rules: {workshop_id}: {raw_rules}") # DEBUG CODE
    rules = raw_rules # return raw llm output
    
    # TODO: Save rules in the DB (Consider adding a field to the Workshop model)
    # Example:
    # workshop = Workshop.query.get(workshop_id)
    # if workshop:
    #     workshop.generated_rules = rules # Assuming you add a 'generated_rules' text field
    #     db.session.commit()

    # Return just the text for this specific route
    return rules

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



# #-----------------------------------------------------------
# # 1.b Generate workshop agenda (New Function)
def generate_agenda_text(workshop_id):
    """Generates a suggested workshop agenda using the LLM."""
    pre_workshop_data = aggregate_pre_workshop_data(workshop_id)
    if not pre_workshop_data:
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
            model_id="ibm/granite-3-3-8b-instruct", # Or another suitable model
            url=Config.WATSONX_URL,
            project_id=Config.WATSONX_PROJECT_ID,
            apikey=Config.WATSONX_API_KEY,
            params={
                "decoding_method": "sample", # Sample might be better for creative agenda structure
                "max_new_tokens": 300,      # Increased for potentially longer agenda
                "min_new_tokens": 50,
                "temperature": 0.7,         # Moderate temperature
                "top_k": 50,
                "top_p": 0.9,
                "repetition_penalty": 1.05
            }
        )

    # Define llm prompt
    agenda_prompt = PromptTemplate.from_template(agenda_prompt_template)

    # Invoke llm chain
    chain = agenda_prompt | watsonx_llm_agenda
    raw_agenda = chain.invoke({"pre_workshop_data": pre_workshop_data})

    print(f"[Agent] Workshop raw agenda for {workshop_id}: {raw_agenda}") # DEBUG CODE

    # Basic cleanup (optional, depends on model consistency)
    # raw_agenda = raw_agenda.strip()

    return raw_agenda

# Optional: API endpoint if needed
@agent_bp.route("/generate_agenda/<int:workshop_id>", methods=["POST"])
@login_required
def generate_agenda(workshop_id):
    """API endpoint to generate and return an agenda."""
    agenda_text = generate_agenda_text(workshop_id)
    if "Could not generate agenda" in agenda_text:
        return jsonify({"error": agenda_text}), 404
    return jsonify({"agenda": agenda_text}), 200



# #-----------------------------------------------------------
# # 1.c Generate workshop action plan (New Function)
def generate_action_plan_text(workshop_id, force: bool = False):
    """
    Generates or retrieves a structured action plan as a valid JSON array.
    Ensures output is clean, structured, and stored in the database.
    """

    workshop = Workshop.query.get_or_404(workshop_id)

    if workshop.generated_action_plan and not force:
        current_app.logger.debug(f"[Agent] Returning cached action plan for workshop {workshop_id}")
        return workshop.generated_action_plan

    current_app.logger.debug(f"[Agent] Generating new action plan for workshop {workshop_id} (force={force})")

    pre_workshop_data = aggregate_pre_workshop_data(workshop_id)
    if not pre_workshop_data:
        return "Could not generate workshop plan: Workshop data unavailable."

    prompt_template = """
You are an expert workshop-design assistant.
Use ONLY the context from the workshop data provided below to create a high-level action plan for the workshop.
Produce a valid JSON **array** whose items are ordered phases of the workshop.
Each item must be an object with:
  • "phase": concise name of the phase
  • "description": ≤15 word explanation
Output MUST be plain JSON only — no Markdown, no comments, no triple backticks.

Workshop Context:
{pre_workshop_data}
"""

    watsonx_llm_action_plan = WatsonxLLM(
        model_id="ibm/granite-3-3-8b-instruct",
        url=Config.WATSONX_URL,
        project_id=Config.WATSONX_PROJECT_ID,
        apikey=Config.WATSONX_API_KEY,
        params={
            "decoding_method": "sample",
            "max_new_tokens": 250,
            "min_new_tokens": 30,
            "temperature": 0.7,
            "top_k": 50,
            "top_p": 0.9,
            "repetition_penalty": 1.1
        }
    )

    action_plan_prompt = PromptTemplate.from_template(prompt_template)
    chain = action_plan_prompt | watsonx_llm_action_plan

    raw_output = chain.invoke({"pre_workshop_data": pre_workshop_data})
    current_app.logger.debug(f"[Agent] Workshop raw action plan for {workshop_id}: {raw_output}")

    cleaned_json_string = extract_json_block(raw_output)
    try:
        parsed = json.loads(cleaned_json_string)

        # Sanity check: Must be a list of objects with expected keys
        if not isinstance(parsed, list) or not all(isinstance(item, dict) and "phase" in item and "description" in item for item in parsed):
            raise ValueError("Invalid structure: Action plan must be a list of objects with 'phase' and 'description'")

        validated_json = json.dumps(parsed, indent=2)
        workshop.generated_action_plan = validated_json
        db.session.commit()

        return validated_json

    except Exception as e:
        current_app.logger.warning(f"[Agent] Plan JSON parse error: {e}. Raw output: {raw_output[:150]}...")
        return "AGENT Could not generate valid JSON action plan."


# API endpoint for action plan generation (This one likely doesn't need the force flag, as it's for initial generation)
@agent_bp.route("/generate_action_plan/<int:workshop_id>", methods=["POST"])
@login_required
def generate_action_plan(workshop_id):
    """API endpoint to generate and return an action plan."""
    # Calls the helper without forcing regeneration by default
    action_plan_text = generate_action_plan_text(workshop_id)
    if "Could not generate" in action_plan_text: # Check for both data unavailable and invalid JSON errors
        return jsonify({"error": action_plan_text}), 500 # Use 500 for server-side generation issues
    # Check if the result is valid JSON before returning
    try:
        json.loads(action_plan_text)
        return jsonify({"action_plan": action_plan_text}), 200
    except json.JSONDecodeError:
        return jsonify({"error": "Generated action plan was not valid JSON."}), 500









# #-----------------------------------------------------------

### WORKSHOP ROOM # #-----------------------------------------
# # This is the area where the workshop takes place.
### #---------------------------------------------------------

# #-----------------------------------------------------------
# # Helper function to extract json block from LLM output
def extract_json_block_______(text):
    """
    Extract JSON object from a Markdown-style fenced LLM output block.
    """
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        return match.group(1)
    return text.strip()  # fallback if no fencing found

def extract_json_block(text):
    """
    Extract JSON array or object from LLM output.
    Removes markdown-style ```json blocks if present.
    """
    array_match = re.search(r"\[.*\]", text, re.DOTALL)
    if array_match:
        return array_match.group(0)
    object_match = re.search(r"\{.*\}", text, re.DOTALL)
    if object_match:
        return object_match.group(0)
    return text.strip()




# #-----------------------------------------------------------
# # 1.b Generate workshop introduction
@agent_bp.route("/generate_introduction_text/<int:workshop_id>", methods=["POST"])
@login_required
def generate_introduction_text(workshop_id):
    """
    Uses the same pre-workshop data + existing rules/agenda to craft:
     - a welcome
     - statement of objectives
     - reinforcement of rules
     - launch instructions for Task #1
    """
    # Aggregate pre-workshop data
    pre_workshop_data = aggregate_pre_workshop_data(workshop_id)
    if not pre_workshop_data:
        # Return raw text directly as the route seems to expect it based on usage
        return "Could not generate introduction: Workshop data unavailable.", 404
    
    # Define prompt template for generating introduction
    introduction_prompt_template = f"""
    You are the workshop facilitator. Based *only* on the workshop context below, craft:
     1) A warm welcome,
     2) A reminder of the goals & rules,
     3) A clear instruction for the first brainstorming question.

    Workshop Context:
    {pre_workshop_data}

    Generate output as valid json with the following keys:
    - welcome: A warm welcome message.
    - goals: A statement of the workshop's goals.
    - rules: A reminder of the workshop rules.
    - instructions: Clear instructions for the first brainstorming question.
    - task: The first brainstorming question.
    - task_type: The type of task (e.g., brainstorming, discussion).
    - task_duration: The time allocated for the task in seconds. (e.g., 120 for 2 minutes).
    - task_description: A brief description of the task.
    """
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
    # Define llm prompt - this now correctly parses the template with the placeholder
    introduction_prompt = PromptTemplate.from_template(introduction_prompt_template,)
    
    # Define the chain
    chain = introduction_prompt | watsonx_llm_introduction
    # Generate introduction
    # Note: The prompt template should be designed to ensure the output is in valid JSON format.
    # If the model outputs JSON, you can parse it directly.
    try:
        raw_introduction = chain.invoke({"pre_workshop_data": pre_workshop_data})
        print(f"[Agent] Workshop raw introduction for {workshop_id}: {raw_introduction}") # DEBUG CODE

        # Optional: Validate/parse JSON if the LLM reliably returns it
        # try:
        #     parsed_intro = json.loads(raw_introduction)
        #     # You might want to return the parsed JSON or specific parts
        #     return jsonify(parsed_intro) 
        # except json.JSONDecodeError:
        #     print("[Agent] Warning: LLM output was not valid JSON.")
        #     # Fallback to returning raw text if JSON parsing fails
        #     return raw_introduction

        return raw_introduction # Return the raw LLM output directly for now

    except Exception as e:
        # Catch potential errors during LLM invocation
        print(f"[Agent] Error invoking LLM chain for workshop {workshop_id}: {e}")
        # You might want to return a more specific error message to the client
        return f"Error generating introduction: {e}", 500

# Optional: API endpoint
@agent_bp.route("/generate_introduction/<int:workshop_id>", methods=["POST"])
@login_required
def generate_introduction(workshop_id):
    """API endpoint to generate and return an agenda."""
    introduction_text = generate_introduction_text(workshop_id)
    if "Could not generate introduction" in introduction_text:
        return jsonify({"error": introduction_text}), 404
    return jsonify({"introduction": introduction_text}), 200



##############################################################
##############################################################
# #-----------------------------------------------------------
# # Remove this when new generate_next_task_text is stable
def generate_next_task_text_____OLD(workshop_id):
    """
    Generates the next brainstorming task as a JSON payload,
    optionally focusing on a specific action_plan_item.
    """
    pre_workshop_data = aggregate_pre_workshop_data(workshop_id)
    if not pre_workshop_data:
        return json.dumps({"error": "Workshop data unavailable."})

    prompt_template = """
You are the facilitator for a brainstorming workshop. Based *only* on the workshop context below,
create the next task. Produce output as a JSON object with these keys:
- title: A very short title for this task.
- task_type: The type of activity (e.g., "brainstorming", "discussion").
- task_description: The question or prompt participants should address.
- instructions: How participants should submit ideas (e.g., “Post on sticky notes…”).
- task_duration: The time allocated for the task in seconds. (e.g., 120 for 2 minutes).

Workshop Context:
{pre_workshop_data}

Respond with *only* valid JSON.
"""
    watsonx = WatsonxLLM(
        model_id="ibm/granite-3-3-8b-instruct",
        url=Config.WATSONX_URL,
        project_id=Config.WATSONX_PROJECT_ID,
        apikey=Config.WATSONX_API_KEY,
        params={
            "decoding_method": "greedy",
            "max_new_tokens": 200,
            "min_new_tokens": 50,
            "temperature": 0.7,
        }
    )
    prompt = PromptTemplate.from_template(prompt_template)
    chain = prompt | watsonx
    raw = chain.invoke({"pre_workshop_data": pre_workshop_data})
    return raw
# #-----------------------------------------------------------
##############################################################
## #-----------------------------------------------------------
# # New next task function

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
        phase_context = "Create the next logical task for the workshop based on the overall context."
    # ---------------------------------------------

    prompt_template = f"""
You are the facilitator for a brainstorming workshop.

Workshop Context:
{{pre_workshop_data}}

{phase_context}

Produce output as a valid JSON object with these keys:
- title: A very short, engaging title for this task (related to the current phase if provided).
- task_type: The type of activity (e.g., "Brainstorming", "Idea Grouping", "Prioritization", "Discussion").
- task_description: The specific question or prompt participants should address for this task. Make it actionable.
- instructions: Clear, concise instructions on how participants should contribute (e.g., "Submit your ideas individually using the input field below.").
- task_duration: Suggested time for this task in SECONDS (e.g., 180 for 3 minutes, 300 for 5 minutes). Be realistic based on the task type.

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

    current_app.logger.debug(f"[Agent] Raw next task for workshop {workshop_id} (Phase: {action_plan_item.get('phase', 'N/A') if action_plan_item else 'Generic'}): {raw}")

    # Return the raw output, route will handle cleaning/parsing
    return raw





##############################################################


# #-----------------------------------------------------------

### VIRTUAL ASSISTANT # #-----------------------------------------
# # LLM-powered architecture with chat interface for the agent.
### #---------------------------------------------------------

# #-----------------------------------------------------------
# # Import libraries
from langgraph.prebuilt import create_react_agent
from langchain.agents import Tool, initialize_agent
from langchain.tools import tool
from langchain_core.prompts import PromptTemplate


from app.extensions import db, socketio
from flask_login import current_user
from datetime import datetime
from app.config import Config
from langchain.schema import HumanMessage, SystemMessage
from langgraph.graph import START, MessagesState, StateGraph
import sqlite3
from langgraph.checkpoint.sqlite import SqliteSaver
#-----------------------------------------------------------
# # Model Setup
# Instantiate the chat model object using IBM watsonx endpoints
llm = ChatWatsonx(
    model_id=Config.WATSONX_MODEL_ID_3,
    url=Config.WATSONX_URL,
    project_id=Config.WATSONX_PROJECT_ID,
    apikey=Config.WATSONX_API_KEY,
    params={
        "decoding_method": "sample",
        "max_new_tokens": 500,
        "temperature": 0.6,
        "top_k": 40,
        "top_p": 0.8,
    }
)
#-----------------------------------------------------------
# # System message template
system_message = """
                You are a virtual meeting assistant.
                """
#-----------------------------------------------------------
# # Conversational workflow
workflow = StateGraph(state_schema=MessagesState)
def call_model(state: MessagesState):
    """
    This function is responsible for sending the current conversation messages
    to the model and returning the model's response. It takes the current state,
    extracts the messages, invokes the model, and then returns the new messages.
    """
    system_msg = SystemMessage(content=system_message)
    # Ensure the system message is the first message in the conversation
    messages = [system_msg] + state["messages"]
    
    response = llm.invoke(messages)
    # Ensure the response is in dictionary format
    if not isinstance(response, dict):
        response = {"message": response}
    
    return {"messages": response}

workflow.add_edge(START, "model")
workflow.add_node("model", call_model)
#-----------------------------------------------------------
# # State Management

# TODO: Move to application factory to have the connection available throughout the app cycle
# TODO: initialize the block below in create_app(): in app factory init.py
# TODO: Implement right after registering the blueprints with app.app_context(): inside create_app()

# construct an absolute path to instance/agent_memory.sqlite
# db_path = os.path.join(current_app.instance_path, 'agent_memory.sqlite')
# conn = sqlite3.connect(db_path, check_same_thread=False)
# memory = SqliteSaver(conn)
# app = workflow.compile(checkpointer=memory)

#-----------------------------------------------------------

# # PDF Retrieval & Q&A Tool
def pdf_qa(input_str: str) -> str:
    """
    Expects a JSON or simple string input with two keys: 
      - "pdf_path": path to the PDF
      - "question": question to ask
    Example input:
        {
            "pdf_path": "path/to/file.pdf",
            "question": "What does the document say about XYZ?"
        }
    """
    try:
        data = json.loads(input_str)
        pdf_path = data.get("pdf_path", "").strip()
        question = data.get("question", "").strip()
        if not pdf_path or not question:
            return "Error: Please provide both 'pdf_path' and 'question'."
    except:
        return ("Error: Input must be valid JSON with 'pdf_path' and 'question' keys. "
                "Example: {\"pdf_path\": \"sample.pdf\", \"question\": \"...\"}")

    # Load and process PDF document
    loader = PyPDFLoader(pdf_path)
    docs = loader.load()

    # Split documents into manageable chunks.
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=100)
    splitted_docs = text_splitter.split_documents(docs)

    # Create embeddings using WatsonxEmbeddings
    embeddings = WatsonxEmbeddings(
        model_id="ibm/slate-125m-english-rtrvr",
        project_id=Config.WATSONX_PROJECT_ID,
    )

    # Build in-memory FAISS vector store for retrieval.
    vector_store = FAISS.from_documents(splitted_docs, embeddings)

    # Setup a retrieval-based Q&A chain with a nested LLM.
    qa_llm = ChatWatsonx(
        model_id="ibm/granite-3-8b-instruct", 
        url=Config.WATSONX_URL,
        project_id=Config.WATSONX_PROJECT_ID,
        # Model Parameters for ChatWatsonx
        params={
            "temperature": 0, 
            "max_tokens": 2500
        },
    )
    qa_chain = RetrievalQA.from_chain_type(
        llm=qa_llm,
        chain_type="stuff",
        retriever=vector_store.as_retriever(),
    )

    # Ask the question with retrieved PDF context
    answer = qa_chain.run(question)
    return answer

# Wrap the PDF Q&A function as a Tool object
pdf_qa_tool = Tool(
    name="PdfQA",
    func=pdf_qa,
    description=(
        "Use this tool to answer questions about the contents of a PDF. "
        "Input must be JSON with 'pdf_path' and 'question' keys."
    ),
)
#-----------------------------------------------------------
# # Action Item Generation Tool
def mark_action_item_tool_func(workshop_id: int, description: str) -> str:
    """
    Adds an action item to the meeting and emits a socket event.
    """
    action_item = ActionItem(
        workshop_id=workshop_id,
        assigned_to=current_user.user_id,
        description=description,
        status="pending",
        priority="medium",
        created_timestamp=datetime.utcnow()
    )
    db.session.add(action_item)
    db.session.commit()

    socketio.emit("action_item_marked", {
        "workshop_id": workshop_id,
        "action_item": {
            "action_item_id": action_item.action_item_id,
            "description": action_item.description,
            "status": action_item.status,
            "priority": action_item.priority
        }
    }, room=f"meeting_{workshop_id}")

    return f"Action item added: {description}"

# Wrap the Mark Action Item Tool as a Tool object
action_item_tool = Tool(
            name="mark_action_item",
            func=lambda description: mark_action_item_tool_func(workshop_id, description),
            description=(
                    "Use this tool to add action items."
                ),
        )
#-----------------------------------------------------------
# # Agent Initialization
def create_agent_executor(workshop_id):
    # Combine All Tools
    tools = [
        pdf_qa_tool,
        action_item_tool
    ]
    # Create the agent executor using the ReACT agent with Model and Tools.
    agent_executor = create_react_agent(llm, tools, checkpointer=memory)
    return agent_executor


#-----------------------------------------------------------
# # Process User Query
def process_user_query(workshop_id, user_query):
    print("PROCESSING USER QUERY") # DEBUG CODE
    meeting, pre_meeting_data = aggregate_pre_meeting_data(workshop_id)
    print("AGGREGATE DATA ACQUIRED")
    agent_executor = create_agent_executor(workshop_id)
    
    prompt = f"""
    You are a helpful virtual assistant for a workshop.

    Meeting Context:
    {pre_meeting_data}

    User Query:
    {user_query}

    Please respond accurately or execute any required actions.
    """
    # Create a state dictionary with a "messages" key
    state = {"messages": [HumanMessage(content=prompt)]}
    config = {"configurable": {"thread_id": workshop_id}}
    
    try:
        result = agent_executor.invoke(state, config)
        
        # If the result contains messages, extract the last message only
        if isinstance(result, dict) and "messages" in result:
            messages = result["messages"]
            if isinstance(messages, list) and messages:
                last_message = messages[-1]
                message_text = last_message.content if hasattr(last_message, "content") else str(last_message)
                print(message_text) # DEBUG CODE
                return message_text  # Return only the last agent reply
                
            elif hasattr(messages, "content"):
                return messages.content
            else:
                return str(messages)
        else:
            return str(result)
    except Exception as e:
        return f"Agent execution error: {str(e)}"
#-----------------------------------------------------------
# # Agent chat message
@agent_bp.route("/chat", methods=["POST"])
# @login_required
def chat():
    data = request.get_json() or {}
    print("AGENT Chat Workshop ID ", data.get("workshop_id", 0))
    print("AGENT Chat User ID ", data.get("user_id", 0))
    user_message = data.get("message", "").strip()
    workshop_id = data.get("workshop_id", 0)
    
    
    if not user_message:
        return jsonify({"error": "Message required."}), 400
    
    
    # Check if current_user is authenticated and has the necessary attributes.
    if hasattr(current_user, "is_authenticated") and current_user.is_authenticated:
        uid = getattr(current_user, "user_id", 0)
        uname = getattr(current_user, "username", "anonymous")
    else:
        uid = 0
        uname = "anonymous"
    
    # Save the user's message in the DB
    chat_msg = ChatMessage(
        workshop_id=workshop_id,
        user_id=uid,
        username=uname,
        message=user_message,
        timestamp=datetime.utcnow()
    )
    db.session.add(chat_msg)
    db.session.commit()
    
    # Use the unified LLM chain to process the query
    agent_response = process_user_query(workshop_id, user_message)
    
    socketio.emit("agent_response", {
        "message": agent_response,
        "type": "unified",
        "username": "Agent"
    }, room=f"meeting_{workshop_id}", namespace="/agent") 
    current_app.logger.info(f"Agent response emitted to meeting_{workshop_id}")

    return jsonify({"ok": True}), 200