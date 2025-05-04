# app/service/routes/feasibility.py
import json
from datetime import datetime
from flask import current_app
from sqlalchemy import func

from app.extensions import db
from app.models import Workshop, BrainstormTask, IdeaCluster, IdeaVote
from app.config import Config, TASK_SEQUENCE
from app.utils.json_utils import extract_json_block
from app.utils.data_aggregation import aggregate_pre_workshop_data
from langchain_ibm import WatsonxLLM
from langchain_core.prompts import PromptTemplate


def generate_feasibility_text(workshop_id: int, clusters_summary: str, phase_context: str):
    """Generates feasibility analysis text using LLM."""
    current_app.logger.debug(f"[Feasibility] Generating text for workshop {workshop_id}")
    pre_workshop_data = aggregate_pre_workshop_data(workshop_id) # Get full context
    if not pre_workshop_data:
        return "Could not generate feasibility report: Workshop data unavailable.", 500

    prompt_template = """
You are a pragmatic analyst assessing the feasibility of ideas generated in a workshop.

Workshop Context:
{pre_workshop_data}

Current Action Plan Context:
{phase_context}

Top Voted Idea Clusters:
{clusters_summary}

Instructions:
1. Analyze the top voted clusters based on the workshop's objective and context.
2. For each top cluster, provide a brief feasibility assessment (e.g., potential challenges, required resources, estimated impact).
3. Format the analysis as a concise Markdown report.
4. Generate a JSON object containing the task details and the feasibility report.

Produce output as a *single* valid JSON object with these keys:
- title: "Review Feasibility Analysis"
- task_type: "results_feasibility"
- task_description: "Review the feasibility analysis of the top-voted clusters and prepare for discussion."
- instructions: "Read the report below. Consider the assessments and think about potential next steps or questions."
- task_duration: The time allocated for the task which is 1 minute, in seconds (e.g., 60 for 1 minute).
- feasibility_report: A string containing the Markdown feasibility report.

Respond with *only* the valid JSON object, nothing else.
"""

    watsonx_llm = WatsonxLLM(
        model_id=Config.WATSONX_MODEL_ID_3, # Use appropriate model
        url=Config.WATSONX_URL,
        project_id=Config.WATSONX_PROJECT_ID,
        apikey=Config.WATSONX_API_KEY,
        params={"decoding_method": "sample", "max_new_tokens": 1000, "min_new_tokens": 100, "temperature": 0.7, "repetition_penalty": 1.0}
    )

    prompt = PromptTemplate.from_template(prompt_template)
    chain = prompt | watsonx_llm

    try:
        raw_output = chain.invoke({
            "pre_workshop_data": pre_workshop_data,
            "phase_context": phase_context,
            "clusters_summary": clusters_summary
        })
        current_app.logger.debug(f"[Feasibility] Raw LLM output for {workshop_id}: {raw_output}")
        return raw_output, 200
    except Exception as e:
        current_app.logger.error(f"[Feasibility] LLM error for workshop {workshop_id}: {e}", exc_info=True)
        return f"Error generating feasibility report: {e}", 500


def get_feasibility_payload(workshop_id: int, previous_task_id: int, phase_context: str):
    """Fetches top clusters, generates report, creates DB record, returns payload."""
    # Get clusters from the previous task, ordered by vote count descending
    top_clusters = db.session.query(
            IdeaCluster, func.count(IdeaVote.id).label('vote_count')
        ).join(IdeaVote, IdeaCluster.id == IdeaVote.cluster_id, isouter=True)\
        .filter(IdeaCluster.task_id == previous_task_id)\
        .group_by(IdeaCluster.id)\
        .order_by(func.count(IdeaVote.id).desc())\
        .limit(3).all() # Get top 3 clusters, adjust as needed

    if not top_clusters:
        return "No voted clusters found from the previous task.", 400

    clusters_summary = "\n".join([f"- {cluster.name} (Votes: {count})" for cluster, count in top_clusters])

    raw_text, code = generate_feasibility_text(workshop_id, clusters_summary, phase_context)
    if code != 200: return raw_text, code
    json_block = extract_json_block(raw_text)
    if not json_block: return "Could not extract valid JSON for feasibility task.", 500

    try:
        payload = json.loads(json_block)
        if not all(k in payload for k in ["title", "task_description", "instructions", "task_duration", "feasibility_report"]): raise ValueError("Missing keys.")
        payload["task_type"] = "results_feasibility"
        task = BrainstormTask(workshop_id=workshop_id, title=payload["title"], prompt=json.dumps(payload), duration=int(payload.get("task_duration", 240)), status="pending")
        db.session.add(task); db.session.flush(); payload['task_id'] = task.id
        current_app.logger.info(f"[Feasibility] Created task {task.id} for workshop {workshop_id}")
        return payload
    except (json.JSONDecodeError, ValueError, TypeError) as e: current_app.logger.error(f"[Feasibility] Payload error {workshop_id}: {e}\nJSON: {json_block}", exc_info=True); db.session.rollback(); return f"Invalid feasibility task format: {e}", 500
    except Exception as e: current_app.logger.error(f"[Feasibility] DB error {workshop_id}: {e}", exc_info=True); db.session.rollback(); return "Server error creating feasibility task.", 500