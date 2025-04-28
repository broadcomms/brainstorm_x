# app/utils/data_aggregation.py

import json
from datetime import datetime
from flask import current_app # Needed for logging/app context
from sqlalchemy.orm import selectinload # Needed for query options

# Import models used by the function
from app.extensions import db # Assuming db is in app.extensions
from app.models import (
    Workshop,
    WorkshopParticipant,
    WorkshopDocument,
    User, # User model is used indirectly via relationships
    Workspace, # Workspace model is used indirectly via relationships
    # Add any other models used directly or indirectly in the function
)

# -----------------------------------------------------------
# 1.a Aggregate pre-workshop data (Moved from agent.py)
def aggregate_pre_workshop_data(workshop_id):
    """
    Aggregates comprehensive data about a workshop, its participants,
    workspace, and linked documents into a structured string format
    suitable for an LLM prompt.
    """
    print(f"[Data Aggregation] Aggregating pre-workshop data for workshop_id: {workshop_id}")

    # 1. Get the Workshop object
    workshop = Workshop.query.options(
        db.selectinload(Workshop.workspace), # Eager load workspace
        db.selectinload(Workshop.creator),   # Eager load creator
        db.selectinload(Workshop.participants).selectinload(WorkshopParticipant.user), # Eager load participants and their users
        db.selectinload(Workshop.linked_documents).selectinload(WorkshopDocument.document) # Eager load linked docs and the actual documents
    ).get(workshop_id)

    if not workshop:
        print(f"[Data Aggregation] Workshop with ID {workshop_id} not found.")
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
    if workshop.rules:
        indented_rules = workshop.rules.replace('\n', '\n    ')
        data_string += f"*   **Rules/Guidelines:**\n    {indented_rules}\n"
    else:
        data_string += "*   **Rules/Guidelines:** Not generated yet.\n"

    if workshop.icebreaker:
        data_string += f"*   **Icebreaker:** {workshop.icebreaker}\n"
    else:
        data_string += "*   **Icebreaker:** Not generated yet.\n"

    if workshop.tip:
        data_string += f"*   **Preparation Tip:** {workshop.tip}\n"
    else:
        data_string += "*   **Preparation Tip:** Not generated yet.\n"
        # --- ADDED: Include Generated Action Plan ---
    if workshop.task_sequence:
        indented_plan = workshop.task_sequence.replace('\n', '\n    ')
        #print(f"[Agent] Workshop action plan: {indented_plan}") # DEBUG CODE
        # data_string += f"*   **Action Plan:**\n    {indented_plan}\n"
        try:
            data = json.loads(indented_plan)
            markdown_output = "# Workshop Phases\n\n"
            for item in data:
                markdown_output += f"## {item.get('phase', 'N/A')}\n{item.get('description', 'No description')}\n\n"
                # --- FIX: Perform replacement before the f-string ---
                indented_markdown = markdown_output.replace('\n', '\n    ')
                data_string += f"*   **Action Plan:**\n    {indented_markdown}\n" # Use the variable here
                # --- End Fix ---
        except json.JSONDecodeError:
             data_string += f"*   **Action Plan:** Invalid JSON stored.\n"
        except Exception as e:
             data_string += f"*   **Action Plan:** Error processing plan ({e}).\n"

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

    print(f"[Data Aggregation] Successfully aggregated data for workshop {workshop_id}.") # DEBUG CODE
    return data_string

