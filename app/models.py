# app/models.py
from datetime import datetime, timedelta
from flask_login import UserMixin
from .extensions import db
import secrets # Added for participants token
import json # Added for whiteboard content


# ---------------- User Model ----------------
class User(db.Model, UserMixin):
    __tablename__ = "users"
    user_id = db.Column(db.Integer, primary_key=True)

    # Basic Identity
    username = db.Column(db.String(100), nullable=True)  # Used for display name
    
    email = db.Column(db.String(255), unique=True, nullable=False)  # For login
    password = db.Column(db.Text, nullable=False)

    # Profile Details
    first_name = db.Column(db.String(100), nullable=True)
    last_name = db.Column(db.String(100), nullable=True)
    job_title = db.Column(db.String(150), nullable=True)
    phone_number = db.Column(db.String(50), nullable=True)
    organization = db.Column(db.String(150), nullable=True)
    # Role Based Access Control (RBAC)
    role = db.Column(db.String(50), default="user")  # 'admin', 'manager', 'user'

    # Email verification
    email_verified = db.Column(db.Boolean, default=False)
    verification_token = db.Column(db.String(255), nullable=True)

    # Password Reset
    reset_token = db.Column(db.String(255), nullable=True)
    reset_token_expires = db.Column(db.DateTime, nullable=True)

    # Profile Picture
    profile_pic_url = db.Column(db.String(255), default="images/default-profile.png")

    # Timestamps
    created_timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    updated_timestamp = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # Relationships
    workspace_memberships = db.relationship("WorkspaceMember", back_populates="user", lazy='dynamic')
    uploaded_documents = db.relationship("Document", back_populates="uploader", lazy='dynamic')
    created_workshops = db.relationship("Workshop", back_populates="creator", foreign_keys="Workshop.created_by_id", lazy='dynamic')
    workshop_participations = db.relationship("WorkshopParticipant", back_populates="user", lazy='dynamic')

    def get_id(self):
        return str(self.user_id)


# ---------------- Workspace Model ----------------
class Workspace(db.Model):
    __tablename__ = "workspaces"
    workspace_id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), unique=True, nullable=False)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=False)
    is_private = db.Column(db.Boolean, default=True)
    created_timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    updated_timestamp = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    description = db.Column(db.Text, nullable=True)
    logo_url = db.Column(db.String(255), default="")

    # Relationships
    owner = db.relationship("User", backref=db.backref("owned_workspaces", lazy=True))
    members = db.relationship("WorkspaceMember", back_populates="workspace", cascade="all, delete-orphan", lazy='selectin')
    documents = db.relationship("Document", back_populates="workspace", cascade="all, delete-orphan", lazy='dynamic')
    workshops = db.relationship("Workshop", back_populates="workspace", cascade="all, delete-orphan", lazy='dynamic')


# ------------- Workspace Member Model ----------------
class WorkspaceMember(db.Model):
    __tablename__ = "workspace_members"
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspaces.workspace_id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=False)
    role = db.Column(db.String(50), default="member")  # RBAC: 'admin', 'member', 'viewer'
    status = db.Column(db.String(50), default="active")  # STATUS: 'active', 'invited','declined','inactive', 'requested'
    joined_timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    user = db.relationship("User", back_populates="workspace_memberships")
    workspace = db.relationship("Workspace", back_populates="members")

    # Unique constraint
    __table_args__ = (db.UniqueConstraint('workspace_id', 'user_id', name='_workspace_user_uc'),)


# ---------------- Member Invitation Model ----------------
class Invitation(db.Model):
    __tablename__ = "invitations"
    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(255), unique=True, nullable=False)
    email = db.Column(db.String(255), nullable=False)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspaces.workspace_id"), nullable=False)
    inviter_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=False)
    sent_timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    expiration_timestamp = db.Column(db.DateTime)
    custom_message = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(50), default='pending', nullable=False) #STATUS: 'pending', 'accepted', 'declined', 'expired'

    # Relationships
    workspace = db.relationship("Workspace", backref=db.backref("invitations", lazy=True))
    inviter   = db.relationship("User",      backref=db.backref("sent_invitations", lazy=True))

    # Helper method to generate token and set expiration ---
    def generate_token(self, expires_in_days=7):
        self.token = secrets.token_urlsafe(32)
        self.expiration_timestamp = datetime.utcnow() + timedelta(days=expires_in_days)

    # Helper method to check if token is valid ---
    def is_valid(self):
        return self.status == 'pending' and self.expiration_timestamp and self.expiration_timestamp > datetime.utcnow()


# ---------------- Document Model ----------------
class Document(db.Model):
    __tablename__ = "documents"
    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspaces.workspace_id"), nullable=False)
    title = db.Column(db.String(255), nullable=False)
    file_name = db.Column(db.String(255), nullable=False) # Original uploaded filename
    file_path = db.Column(db.String(255), nullable=False) # Path relative to instance/uploads
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=False)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)
    file_size = db.Column(db.Integer, nullable=True) # Store file size in bytes
    description = db.Column(db.Text, nullable=True) # <-- ADDED THIS FIELD

    # Relationships
    uploader = db.relationship("User", back_populates="uploaded_documents")
    workspace = db.relationship("Workspace", back_populates="documents")
    workshop_links = db.relationship("WorkshopDocument", back_populates="document", cascade="all, delete-orphan", lazy='dynamic')


# ---------------- Workshop Model ----------------
class Workshop(db.Model):
    __tablename__ = "workshops"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    objective = db.Column(db.Text, nullable=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspaces.workspace_id"), nullable=False)
    date_time = db.Column(db.DateTime, nullable=False)
    duration = db.Column(db.Integer, nullable=True) # Duration in minutes
    status = db.Column(db.String(50), default="scheduled") #STATUS: 'scheduled', 'inprogress', 'paused', 'completed', 'cancelled'
    agenda = db.Column(db.Text, nullable=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    rules = db.Column(db.Text, nullable=True) # JSON or text representation of generated rules
    icebreaker = db.Column(db.Text, nullable=True) # JSON or text representation of generated icebreaker
    tip = db.Column(db.Text, nullable=True) # JSON or text representation of generated tips
    
    # --- MODIFIED/ADDED FOR PERSISTENCE ---
    # Link to the currently active task
    current_task_id = db.Column(db.Integer, db.ForeignKey("brainstorm_tasks.id"), nullable=True)

    # Timer state tracking
    timer_start_time = db.Column(db.DateTime, nullable=True) # When the current active period started (start or resume)
    timer_paused_at = db.Column(db.DateTime, nullable=True) # Timestamp of the last pause
    timer_elapsed_before_pause = db.Column(db.Integer, default=0) # Seconds elapsed before the last pause

    # Store the sequence of tasks (e.g., from action plan) - Keep this if used for task generation
    task_sequence = db.Column(db.Text, nullable=True)
    current_task_index = db.Column(db.Integer, nullable=True, default=None) # Index within task_sequence

    # Whiteboard content (optional, alternative is querying ideas)
    # whiteboard_content = db.Column(db.Text, nullable=True) # Example: Store as JSON string
    # --- END MODIFIED/ADDED FOR PERSISTENCE ---

    # Relationships
    tasks = db.relationship(
        "BrainstormTask",
        back_populates="workshop",
        cascade="all, delete-orphan",
        lazy='select',
        # Explicitly state the foreign key column(s) in the *child* table (BrainstormTask)
        # that link back to *this* parent table (Workshop).
        foreign_keys="BrainstormTask.workshop_id"
    )
    workspace = db.relationship("Workspace", back_populates="workshops")
    creator = db.relationship("User", back_populates="created_workshops", foreign_keys=[created_by_id])
    participants = db.relationship("WorkshopParticipant", back_populates="workshop", cascade="all, delete-orphan", lazy='dynamic')
    linked_documents = db.relationship("WorkshopDocument", back_populates="workshop", cascade="all, delete-orphan", lazy='dynamic')
    chat_messages = db.relationship("ChatMessage", back_populates="workshop", cascade="all, delete-orphan", lazy='dynamic', order_by="ChatMessage.timestamp")

    # Relationship to the current task object
    current_task = db.relationship("BrainstormTask", foreign_keys=[current_task_id], post_update=True) # Removed remote_side for simplicity if not strictly needed

    # Helper property to get the organizer
    @property
    def organizer(self):
        # Assuming organizer is always the creator for simplicity now
        return self.creator
        # Alternative if using role:
        # organizer_participant = self.participants.filter_by(role='organizer').first()
        # return organizer_participant.user if organizer_participant else None

    # --- ADDED: Helper to get remaining time ---
    def get_remaining_task_time(self) -> int:
        """Calculates remaining seconds for the current task, returns 0 if no task/timer."""
        if not self.current_task or not self.current_task.duration:
            return 0

        if self.status == 'paused' and self.timer_paused_at:
            # If paused, remaining time is total duration minus what elapsed before pause
            total_elapsed = self.timer_elapsed_before_pause
        elif self.status == 'inprogress' and self.timer_start_time:
            # If running, calculate elapsed time in current run + time before pause
            elapsed_this_run = (datetime.utcnow() - self.timer_start_time).total_seconds()
            total_elapsed = self.timer_elapsed_before_pause + elapsed_this_run
        else:
            # No timer running or invalid state
            return 0

        remaining = self.current_task.duration - total_elapsed
        return max(0, int(remaining)) # Return non-negative integer



# ---------------- Workshop Participant Model ----------------
class WorkshopParticipant(db.Model):
    __tablename__ = "workshop_participants"
    id = db.Column(db.Integer, primary_key=True)
    workshop_id = db.Column(db.Integer, db.ForeignKey("workshops.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=False)
    role = db.Column(db.String(50), default="participant") # organizer, participant
    status = db.Column(db.String(50), default="invited") # invited, accepted, declined
    invitation_token = db.Column(db.String(64), unique=True, nullable=True) # Token for accept/decline link
    token_expires = db.Column(db.DateTime, nullable=True) # Expiration for the token
    joined_timestamp = db.Column(db.DateTime, nullable=True) # When they accepted

    # --- ADDED FOR VOTING ---
    dots_remaining = db.Column(db.Integer, default=5) # Example: Start with 5 dots
    # ------------------------


    # Relationships
    workshop = db.relationship("Workshop", back_populates="participants")
    user = db.relationship("User", back_populates="workshop_participations")
    submitted_ideas = db.relationship("BrainstormIdea", back_populates="participant", cascade="all, delete-orphan", lazy='dynamic') # Added backref
    votes_cast = db.relationship("IdeaVote", back_populates="participant", cascade="all, delete-orphan", lazy='dynamic') # Added backref


    # Unique constraint
    __table_args__ = (db.UniqueConstraint('workshop_id', 'user_id', name='_workshop_user_uc'),)

    # Helper function to generate and validate tokens.
    def generate_token(self):
        self.invitation_token = secrets.token_urlsafe(32)
        self.token_expires = datetime.utcnow() + timedelta(days=7) # Example: 7-day expiry
    def is_token_valid(self):
        return self.invitation_token and self.token_expires and self.token_expires > datetime.utcnow()


# ---------------- Workshop Document Link Model ----------------
class WorkshopDocument(db.Model):
    __tablename__ = "workshop_documents"
    id = db.Column(db.Integer, primary_key=True)
    workshop_id = db.Column(db.Integer, db.ForeignKey("workshops.id"), nullable=False)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=False)
    added_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    workshop = db.relationship("Workshop", back_populates="linked_documents")
    document = db.relationship("Document", back_populates="workshop_links")

    # Unique constraint
    __table_args__ = (db.UniqueConstraint('workshop_id', 'document_id', name='_workshop_document_uc'),)
    
    
# ---------------- BrainstormTask Model ---------------------------
class BrainstormTask(db.Model):
    __tablename__ = "brainstorm_tasks"

    # Task details
    id = db.Column(db.Integer, primary_key=True)
    workshop_id = db.Column(db.Integer, db.ForeignKey("workshops.id"), nullable=False)
    title = db.Column(db.String(255), nullable=False)            # e.g. "Introduction"
    prompt = db.Column(db.Text, nullable=True)                   # The LLM’s generated text/instructions/question OR full JSON payload

    # Timer
    duration = db.Column(db.Integer, nullable=False)             # The task duration in seconds
    status = db.Column(db.String(50), default="pending")         # STATUS: 'pending', 'running','completed', 'skipped'
    started_at = db.Column(db.DateTime, nullable=True)
    ended_at = db.Column(db.DateTime, nullable=True)

    # Relationship
    workshop = db.relationship("Workshop", back_populates="tasks", foreign_keys=[workshop_id]) # Explicit FK here too for clarity, matching Workshop.tasks
    ideas = db.relationship("BrainstormIdea", back_populates="task",
                            cascade="all, delete-orphan", lazy="dynamic", order_by="BrainstormIdea.timestamp")



# ---------------- BrainstormIdea Model ---------------------------
class BrainstormIdea(db.Model):
    __tablename__ = "brainstorm_ideas"
    
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("brainstorm_tasks.id"), nullable=False)
    participant_id = db.Column(db.Integer, db.ForeignKey("workshop_participants.id"), nullable=False)
    content = db.Column(db.Text, nullable=False)
    # votes = db.relationship("IdeaVote", back_populates="idea", cascade="all, delete-orphan", lazy='dynamic')
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    
    # --- ADDED/MODIFIED FOR CLUSTERING ---
    cluster_id = db.Column(db.Integer, db.ForeignKey("idea_clusters.id"), nullable=True)
    cluster = db.relationship("IdeaCluster", back_populates="ideas")
    # -----------------------------------
    
    
    # Relationships
    task = db.relationship("BrainstormTask", back_populates="ideas")
    participant = db.relationship("WorkshopParticipant", back_populates="submitted_ideas") # Use back_populates
    # votes = db.relationship("IdeaVote", back_populates="idea", cascade="all, delete-orphan", lazy='dynamic') # Added backref

    
    # Remove Idea
    # cluster_id = db.Column(db.Integer, db.ForeignKey("idea_clusters.id"), nullable=True)
    # cluster = db.relationship("IdeaCluster", back_populates="ideas")
    
    # ... (Remove IdeaCluster, IdeaVote, ActivityLog, SubmittedIdea, WorkshopTask if not used for core persistence) ...
    # Keep ChatMessage as it's part of the persistence requirement
    

# --- ADDED IdeaCluster model definition based on previous context ---
class IdeaCluster(db.Model):
    __tablename__ = "idea_clusters"
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("brainstorm_tasks.id"), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=True) # Optional description
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    ideas = db.relationship("BrainstormIdea", back_populates="cluster", lazy='dynamic')
    task = db.relationship("BrainstormTask") # Relationship back to the voting task
    votes = db.relationship("IdeaVote", back_populates="cluster", cascade="all, delete-orphan", lazy='dynamic') # Votes for this cluster




# --- ADDED IdeaVote model definition based on previous context ---
class IdeaVote(db.Model):
    __tablename__ = "idea_votes"
    id = db.Column(db.Integer, primary_key=True)
    cluster_id = db.Column(db.Integer, db.ForeignKey("idea_clusters.id"), nullable=False)
    participant_id = db.Column(db.Integer, db.ForeignKey("workshop_participants.id"), nullable=False)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # --- MODIFIED: Unique constraint per participant per cluster ---
    __table_args__ = (db.UniqueConstraint('cluster_id', 'participant_id', name='_cluster_participant_uc'),)


    # Relationships
    # idea = db.relationship("BrainstormIdea", back_populates="votes") # Remove if voting on clusters
    cluster = db.relationship("IdeaCluster", back_populates="votes") # Link to cluster
    participant = db.relationship("WorkshopParticipant", back_populates="votes_cast") # Use back_populates



# --- ADDED ActivityLog model definition based on previous context ---
class ActivityLog(db.Model):
    __tablename__ = "activity_logs"
    id = db.Column(db.Integer, primary_key=True)
    participant_id = db.Column(db.Integer, db.ForeignKey("workshop_participants.id"), nullable=True)
    task_id = db.Column(db.Integer, db.ForeignKey("brainstorm_tasks.id"), nullable=True)
    idea_id = db.Column(db.Integer, db.ForeignKey("brainstorm_ideas.id"), nullable=True)
    vote_id = db.Column(db.Integer, db.ForeignKey("idea_votes.id"), nullable=True)
    action = db.Column(db.String(100), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships (Assuming related models have back_populates='logs')
    participant = db.relationship("WorkshopParticipant") # Add back_populates="logs" in WorkshopParticipant if needed
    task        = db.relationship("BrainstormTask") # Add back_populates="logs" in BrainstormTask if needed
    idea        = db.relationship("BrainstormIdea") # Add back_populates="logs" in BrainstormIdea if needed
    vote        = db.relationship("IdeaVote") # Add back_populates="logs" in IdeaVote if needed



# ---------------- ChatMessage Model ---------------------------
class ChatMessage(db.Model):
    __tablename__ = "chat_messages"

    id = db.Column(db.Integer, primary_key=True)
    workshop_id = db.Column(db.Integer, db.ForeignKey("workshops.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=False)
    username = db.Column(db.String(100), nullable=False)
    message = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    workshop = db.relationship("Workshop", back_populates="chat_messages")
    user = db.relationship("User")
    

