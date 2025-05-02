# app/service/routes/summary.py
import json
from datetime import datetime
from flask import current_app

from app.extensions import db
from app.models import Workshop, BrainstormTask, BrainstormIdea, IdeaCluster, IdeaVote, ChatMessage
from app.config import Config, TASK_SEQUENCE
from app.utils.json_utils import extract_json_block
from app.utils.data_aggregation import aggregate_pre_workshop_data
from langchain_ibm import WatsonxLLM
from sqlalchemy import func # <--- Import func
from langchain_core.prompts import PromptTemplate
from sqlalchemy.orm import joinedload


def generate_summary_text(workshop_id: int, phase_context: str):
    """Generates workshop summary text using LLM."""
    current_app.logger.debug(f"[Summary] Generating text for workshop {workshop_id}")

    # --- Aggregate More Data for Summary ---
    # Start with pre-workshop data
    summary_context = aggregate_pre_workshop_data(workshop_id)
    if not summary_context:
        return "Could not generate summary: Workshop data unavailable.", 500

    # Add Ideas, Clusters (with votes), and Chat Messages
    ideas = BrainstormIdea.query.filter(BrainstormIdea.task.has(workshop_id=workshop_id)).all()
    # Query clusters and their vote counts using func.count and group_by
    clusters_with_counts = db.session.query(
            IdeaCluster, func.count(IdeaVote.id).label('vote_count')
        ).outerjoin(IdeaVote, IdeaCluster.id == IdeaVote.cluster_id) \
         .filter(IdeaCluster.task.has(workshop_id=workshop_id)) \
         .group_by(IdeaCluster.id) \
         .all()
    chat_messages = ChatMessage.query.filter_by(workshop_id=workshop_id).order_by(ChatMessage.timestamp).all()

    summary_context += "\n\n**Workshop Activity:**\n"
    if ideas:
        summary_context += f"*   **Ideas Generated ({len(ideas)}):**\n" + "\n".join([f"    - {idea.content[:80]}..." for idea in ideas[:10]]) + ("\n    - ..." if len(ideas) > 10 else "") + "\n"
    if clusters_with_counts:
        summary_context += f"*   **Clusters Discussed ({len(clusters_with_counts)}):**\n" + "\n".join([f"    - {cluster.name} (Votes: {count})" for cluster, count in clusters_with_counts]) + "\n" # Use the count from the query
    
    
    if chat_messages:
         summary_context += f"*   **Chat Snippets ({len(chat_messages)}):**\n" + "\n".join([f"    - {msg.username}: {msg.message[:60]}..." for msg in chat_messages[-5:]]) + "\n" # Last 5 messages
    # --------------------------------------

    prompt_template = """
You are the workshop facilitator, responsible for summarizing the entire session.

Workshop Context and Activity:
{summary_context}

Current Action Plan Context (Final Phase):
{phase_context}

Instructions:
1. Review all the provided context, including initial objectives, generated ideas, cluster votes, and chat snippets.
2. Synthesize the key outcomes, decisions, and any potential action items identified during the workshop.
3. Format the summary as a concise Markdown report suitable for sharing. Include sections like "Key Outcomes", "Decisions Made", "Next Steps/Action Items".
4. Generate a JSON object containing the final task details and the summary report.

Produce output as a *single* valid JSON object with these keys:
- title: "Workshop Summary"
- task_type: "summary"
- task_description: "Here is a summary of the workshop session."
- instructions: "Thank you for your participation! The workshop is now complete."
- task_duration: Suggested time in SECONDS for review (e.g., 120 for 2 mins).
- summary_report: A string containing the Markdown summary report.

Respond with *only* the valid JSON object, nothing else.
"""

    watsonx_llm = WatsonxLLM(
        model_id=Config.WATSONX_MODEL_ID_3, # Use appropriate model
        url=Config.WATSONX_URL,
        project_id=Config.WATSONX_PROJECT_ID,
        apikey=Config.WATSONX_API_KEY,
        params={"decoding_method": "sample", "max_new_tokens": 1500, "min_new_tokens": 150, "temperature": 0.6, "repetition_penalty": 1.0}
    )

    prompt = PromptTemplate.from_template(prompt_template)
    chain = prompt | watsonx_llm

    try:
        raw_output = chain.invoke({"summary_context": summary_context, "phase_context": phase_context})
        current_app.logger.debug(f"[Summary] Raw LLM output for {workshop_id}: {raw_output}")
        return raw_output, 200
    except Exception as e:
        current_app.logger.error(f"[Summary] LLM error for workshop {workshop_id}: {e}", exc_info=True)
        return f"Error generating workshop summary: {e}", 500


def get_summary_payload(workshop_id: int, phase_context: str):
    """Generates text, creates DB record, returns payload."""
    raw_text, code = generate_summary_text(workshop_id, phase_context)
    if code != 200: return raw_text, code
    json_block = extract_json_block(raw_text)
    if not json_block: return "Could not extract valid JSON for summary task.", 500
    try:
        payload = json.loads(json_block)
        if not all(k in payload for k in ["title", "task_description", "instructions", "task_duration", "summary_report"]): raise ValueError("Missing keys.")
        payload["task_type"] = "summary"
        task = BrainstormTask(workshop_id=workshop_id, title=payload["title"], prompt=json.dumps(payload), duration=int(payload.get("task_duration", 120)), status="pending")
        db.session.add(task); db.session.flush(); payload['task_id'] = task.id
        current_app.logger.info(f"[Summary] Created task {task.id} for workshop {workshop_id}")
        # Note: Workshop status is set to 'completed' in the stop_workshop route usually.
        return payload
    except (json.JSONDecodeError, ValueError, TypeError) as e: current_app.logger.error(f"[Summary] Payload error {workshop_id}: {e}\nJSON: {json_block}", exc_info=True); db.session.rollback(); return f"Invalid summary task format: {e}", 500
    except Exception as e: current_app.logger.error(f"[Summary] DB error {workshop_id}: {e}", exc_info=True); db.session.rollback(); return "Server error creating summary task.", 500