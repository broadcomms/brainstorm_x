# app/service/routes/clustering.py
import json
from datetime import datetime
from flask import current_app

from app.extensions import db
from app.models import Workshop, BrainstormTask, BrainstormIdea, IdeaCluster, WorkshopParticipant
from app.config import Config, TASK_SEQUENCE
from app.utils.json_utils import extract_json_block
from app.utils.data_aggregation import aggregate_pre_workshop_data
from langchain_ibm import WatsonxLLM
from langchain_core.prompts import PromptTemplate


def generate_clustering_text(workshop_id: int, ideas_text: str, phase_context: str):
    """Generates clusters and voting task text using LLM."""
    current_app.logger.debug(f"[Clustering] Generating text for workshop {workshop_id}")
    pre_workshop_data = aggregate_pre_workshop_data(workshop_id) # Get full context
    if not pre_workshop_data:
        return "Could not generate clustering task: Workshop data unavailable.", 500

    prompt_template = """
You are the workshop facilitator. You need to group the submitted ideas into meaningful clusters and set up a voting phase.

Workshop Context:
{pre_workshop_data}

Current Action Plan Context:
{phase_context}

Submitted Ideas:
{ideas_text}

Instructions:
1. Analyze the submitted ideas and group them into 3-7 distinct clusters based on common themes or topics.
2. For each cluster, provide a concise, descriptive name.
3. Generate a JSON object containing the task details and the clusters.

Produce output as a *single* valid JSON object with these keys:
- title: "Vote on Idea Clusters"
- task_type: "clustering_voting"
- task_description: "Ideas have been grouped into clusters. Review them and prepare to vote."
- instructions: "Use your assigned dots to vote for the clusters you find most promising. Click a cluster's vote button."
- task_duration: Suggested time in SECONDS for voting (e.g., 180 for 3 mins, 300 for 5 mins).
- clusters: An array of cluster objects. Each cluster object must have:
    - name: A concise name for the cluster (string).
    - description: A brief explanation of the cluster's theme (string, optional).
    - idea_indices: An array of the original 0-based indices of the ideas belonging to this cluster (array of numbers).

Respond with *only* the valid JSON object, nothing else. Ensure 'idea_indices' refers to the 0-based index from the input list.
"""

    watsonx_llm = WatsonxLLM(
        model_id=Config.WATSONX_MODEL_ID_2, # Use appropriate model
        url=Config.WATSONX_URL,
        project_id=Config.WATSONX_PROJECT_ID,
        apikey=Config.WATSONX_API_KEY,
        params={"decoding_method": "greedy", "max_new_tokens": 1500, "min_new_tokens": 100, "temperature": 0.5, "repetition_penalty": 1.05} # More tokens for clustering
    )

    prompt = PromptTemplate.from_template(prompt_template)
    chain = prompt | watsonx_llm

    try:
        raw_output = chain.invoke({
            "pre_workshop_data": pre_workshop_data,
            "phase_context": phase_context,
            "ideas_text": ideas_text
        })
        current_app.logger.debug(f"[Clustering] Raw LLM output for {workshop_id}: {raw_output}")
        return raw_output, 200
    except Exception as e:
        current_app.logger.error(f"[Clustering] LLM error for workshop {workshop_id}: {e}", exc_info=True)
        return f"Error generating clustering task: {e}", 500


def get_clustering_voting_payload(workshop_id: int, previous_task_id: int, phase_context: str):
    """Fetches ideas, generates clusters, creates DB records, returns payload."""
    ideas = BrainstormIdea.query.filter_by(task_id=previous_task_id).order_by(BrainstormIdea.id).all()
    if not ideas:
        return "No ideas found from the previous task to cluster.", 400

    # Create indexed text for LLM
    ideas_text = "\n".join([f"{idx}: {idea.content}" for idx, idea in enumerate(ideas)])
    idea_map = {idx: idea.id for idx, idea in enumerate(ideas)} # Map index back to idea ID

    raw_text, code = generate_clustering_text(workshop_id, ideas_text, phase_context)
    if code != 200:
        return raw_text, code

    json_block = extract_json_block(raw_text)
    if not json_block:
        return "Could not extract valid JSON for clustering task.", 500

    try:
        payload = json.loads(json_block)
        if not all(k in payload for k in ["title", "task_description", "instructions", "task_duration", "clusters"]) or not isinstance(payload.get("clusters"), list):
            raise ValueError("Missing required keys or invalid cluster format in clustering JSON.")
        payload["task_type"] = "clustering_voting"

        # --- Create Voting Task DB Record ---
        task = BrainstormTask(
            workshop_id=workshop_id,
            title=payload["title"],
            prompt=json.dumps(payload), # Store full payload
            duration=int(payload.get("task_duration", 180)), # Default 3 mins
            status="pending"
        )
        db.session.add(task)
        db.session.flush() # Get task ID
        payload['task_id'] = task.id

        # --- Create Cluster DB Records and Link Ideas ---
        processed_clusters = []
        for cluster_data in payload["clusters"]:
            cluster_name = cluster_data.get("name", "Unnamed Cluster")
            cluster_desc = cluster_data.get("description")
            idea_indices = cluster_data.get("idea_indices", [])

            cluster = IdeaCluster(task_id=task.id, name=cluster_name, description=cluster_desc)
            db.session.add(cluster)
            db.session.flush() # Get cluster ID

            # Link ideas using the map
            linked_idea_ids = []
            for idx in idea_indices:
                if idx in idea_map:
                    idea_id = idea_map[idx]
                    idea = next((i for i in ideas if i.id == idea_id), None)
                    if idea:
                        idea.cluster_id = cluster.id
                        linked_idea_ids.append(idea.id)

            processed_clusters.append({
                "id": cluster.id, # Use DB ID
                "name": cluster.name,
                "description": cluster.description,
                "idea_ids": linked_idea_ids # Store actual idea IDs linked
            })

        payload['clusters'] = processed_clusters # Replace LLM clusters with DB-backed clusters
        payload['prompt'] = json.dumps(payload) # Update prompt in task with new cluster IDs

        # --- Add Participant Dot Info ---
        participants_data = WorkshopParticipant.query.filter_by(workshop_id=workshop_id, status='accepted').all()
        # Reset dots for participants (e.g., to 5) - Adjust as needed
        default_dots = 5
        for p in participants_data:
            p.dots_remaining = default_dots
        payload['participants_dots'] = {part.user_id: default_dots for part in participants_data}

        # DO NOT COMMIT HERE - route commits
        current_app.logger.info(f"[Clustering] Created task {task.id} and {len(processed_clusters)} clusters for workshop {workshop_id}")
        return payload

    except (json.JSONDecodeError, ValueError, TypeError) as e:
        current_app.logger.error(f"[Clustering] Payload processing error for workshop {workshop_id}: {e}\nJSON Block: {json_block}", exc_info=True)
        db.session.rollback()
        return f"Invalid clustering task format: {e}", 500
    except Exception as e:
        current_app.logger.error(f"[Clustering] Unexpected error creating task/clusters for workshop {workshop_id}: {e}", exc_info=True)
        db.session.rollback()
        return "Server error creating clustering task.", 500