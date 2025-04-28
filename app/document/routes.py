# app/document/routes.py
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, abort
from flask_login import login_required, current_user
from app.models import Document, Workspace, User, WorkspaceMember # Import WorkspaceMember
from app.extensions import db
import os
from werkzeug.utils import secure_filename
from datetime import datetime
from sqlalchemy.orm import joinedload # To efficiently load related objects

document_bp = Blueprint('document_bp', __name__, template_folder="templates")

# --- Helper to ensure upload directory exists ---
def ensure_upload_dir():
    # Use instance_path for user-uploaded content
    upload_folder = os.path.join(current_app.instance_path, 'uploads', 'documents')
    os.makedirs(upload_folder, exist_ok=True)
    return upload_folder

# --- List Documents Route ---
@document_bp.route('/list', methods=['GET'])
@login_required # Protect this route
def list_documents():
    """
    Lists documents from workspaces the current user is a member of.
    Also provides the list of workspaces for the upload form dropdown.
    """
    # Get IDs of workspaces the user is a member of
    user_workspace_ids = [
        membership.workspace_id
        for membership in current_user.workspace_memberships.filter(WorkspaceMember.status == 'active').all() # Query dynamic relationship
    ]

    # Fetch documents belonging to those workspaces, ordered by upload date
    # Use joinedload to avoid N+1 query problems when accessing document.workspace.name and document.uploader.first_name in the template
    documents = Document.query.options(
            joinedload(Document.workspace),
            joinedload(Document.uploader)
        ).filter(
            Document.workspace_id.in_(user_workspace_ids)
        ).order_by(Document.uploaded_at.desc()).all()

    # Fetch the actual workspace objects for the dropdown
    user_workspaces = Workspace.query.filter(
        Workspace.workspace_id.in_(user_workspace_ids)
    ).order_by(Workspace.name).all()

    return render_template('document_list.html', documents=documents, workspaces=user_workspaces)


# --- Document Upload Route ---
@document_bp.route('/upload', methods=['POST'])
@login_required # Protect this route
def upload_document():
    """Handles the document upload form submission."""
    if 'file' not in request.files:
        flash('No file part selected.', 'danger')
        return redirect(url_for('document_bp.list_documents'))

    file = request.files['file']
    if file.filename == '':
        flash('No file selected.', 'danger')
        return redirect(url_for('document_bp.list_documents'))

    # Get form data
    title = request.form.get('title', '').strip()
    description = request.form.get('description', '').strip() # Get description
    workspace_id = request.form.get('workspace_id', type=int) # Get workspace ID

    # --- Validation ---
    if not workspace_id:
         flash('Please select a workspace.', 'danger')
         return redirect(url_for('document_bp.list_documents'))

    # Verify user is a member of the selected workspace
    is_member = current_user.workspace_memberships.filter_by(
        workspace_id=workspace_id,
        status='active'
    ).first()
    if not is_member:
        flash('You do not have permission to upload to this workspace.', 'danger')
        return redirect(url_for('document_bp.list_documents'))

    if file:
        original_filename = secure_filename(file.filename)
        if not title:
            title = original_filename # Use filename if title is empty

        upload_folder = ensure_upload_dir()
        # Create a unique filename to prevent overwrites and potential conflicts
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S%f") # Added microseconds for more uniqueness
        # Consider adding workspace_id to filename if needed for organization
        unique_filename = f"{current_user.user_id}_{workspace_id}_{timestamp}_{original_filename}"
        file_path_abs = os.path.join(upload_folder, unique_filename)
        # Store path relative to instance folder
        file_path_rel = os.path.join('uploads', 'documents', unique_filename)

        try:
            file.save(file_path_abs)
            file_size = os.path.getsize(file_path_abs) # Get file size after saving

            # Create a new document instance
            new_document = Document(
                title=title,
                description=description, # Add description
                file_name=original_filename,
                file_path=file_path_rel, # Store relative path
                uploaded_by_id=current_user.user_id,
                file_size=file_size,
                workspace_id=workspace_id # Use selected workspace_id
            )
            db.session.add(new_document)
            db.session.commit()
            flash(f'Document "{title}" uploaded successfully to workspace!', 'success')

        except Exception as e:
            db.session.rollback() # Rollback db changes on error
            current_app.logger.error(f"Error uploading document: {e}")
            flash('An error occurred during upload. Please try again.', 'danger')
            # Clean up the saved file if the database operation failed
            if os.path.exists(file_path_abs):
                try:
                    os.remove(file_path_abs)
                except OSError as rm_err:
                     current_app.logger.error(f"Error removing failed upload file {file_path_abs}: {rm_err}")

        return redirect(url_for('document_bp.list_documents'))

    # Fallback redirect if 'file' object somehow doesn't evaluate to True
    return redirect(url_for('document_bp.list_documents'))


# --- Document Preview Route ---
@document_bp.route('/preview/<int:document_id>', methods=['GET'])
@login_required # Protect this route
def preview_document(document_id):
    """Displays details of a single document."""
    # Fetch document and eagerly load related workspace and uploader
    document = Document.query.options(
        joinedload(Document.workspace),
        joinedload(Document.uploader)
    ).get_or_404(document_id)

    # --- Permission Check ---
    # Verify user is a member of the workspace this document belongs to
    is_member = current_user.workspace_memberships.filter_by(
        workspace_id=document.workspace_id,
        status='active'
    ).first()
    if not is_member:
        flash("You don't have permission to view this document.", "danger")
        return redirect(url_for('document_bp.list_documents'))

    # Construct the absolute path to check if the file exists (optional but good practice)
    # Note: This doesn't serve the file, just checks existence. Serving files requires a dedicated route.
    full_file_path = os.path.join(current_app.instance_path, document.file_path)
    file_exists = os.path.exists(full_file_path)
    if not file_exists:
         flash("The document file seems to be missing.", "warning")
         # Decide if you still want to show the preview page or redirect

    return render_template('document_preview.html', document=document, file_exists=file_exists)


# --- Document Delete Route ---
@document_bp.route('/delete/<int:document_id>', methods=['POST']) # Use POST for destructive actions
@login_required # Protect this route
def delete_document(document_id):
    """Deletes a document record and its associated file."""
    document = Document.query.get_or_404(document_id)

    # --- Permission Check ---
    # User must either be the uploader OR an admin/manager of the workspace
    is_owner = document.uploaded_by_id == current_user.user_id
    member_info = current_user.workspace_memberships.filter_by(
        workspace_id=document.workspace_id,
        status='active'
    ).first()
    is_workspace_admin = member_info and member_info.role in ['admin', 'manager'] # Adjust roles as needed

    if not (is_owner or is_workspace_admin):
        flash("You don't have permission to delete this document.", "danger")
        return redirect(url_for('document_bp.list_documents'))

    try:
        # Construct absolute path to the file in the instance folder
        full_file_path = os.path.join(current_app.instance_path, document.file_path)

        # Delete the physical file first (if it exists)
        file_deleted = False
        if os.path.exists(full_file_path):
            try:
                os.remove(full_file_path)
                file_deleted = True
            except OSError as e:
                current_app.logger.error(f"Error deleting file {full_file_path}: {e}")
                # Decide if you want to stop or continue if file deletion fails
                flash('Could not delete the physical file, but will remove the record.', 'warning')

        # Delete the database record
        db.session.delete(document)
        db.session.commit()
        flash(f'Document "{document.title}" deleted successfully.', 'success')

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error deleting document DB record {document_id}: {e}")
        flash('An error occurred while deleting the document record.', 'danger')

    return redirect(url_for('document_bp.list_documents'))
