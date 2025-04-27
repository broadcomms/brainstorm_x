#!/usr/bin/env bash
set -e

# 1) Directories to create
dirs=(
    # app
    app/main
    app/main/templates
    app/auth
    app/auth/templates
    app/account
    app/account/templates
    app/workspace
    app/workspace/templates
    app/document
    app/document/templates
    app/workshop
    app/workshop/templates
    app/chat
    app/chat/templates
    app/service/routes
    app/service/templates
    instance
    instance/documents
    instance/default
    instance/uploads
)

# 2) Files to touch
files=(
    app/__init__.py
    app/config.py
    app/models.py
    app/extensions.py
    app/sockets.py
    app/main/routes.py
    app/auth/routes.py
    app/account/routes.py
    app/workspace/routes.py
    app/document/routes.py
    app/workshop/routes.py
    app/chat/routes.py
    app/service/routes/agenda.py
    app/service/routes/plan.py
    app/service/routes/rules.py
    app/service/routes/icebreaker.py
    app/service/routes/tip.py
    app/service/routes/task.py
    app/service/routes/nudge.py
    app/service/routes/vote.py
    app/service/routes/idea.py
    app/service/routes/agent.py
    # run.py
    # requirements.txt
)

# 3) HTML templates to touch
templates=(
    app/main/templates/main_template.html
    app/main/templates/main_index.html
    app/auth/templates/auth_login.html
    app/auth/templates/auth_password.html
    app/auth/templates/auth_reset.html
    app/account/templates/account_form.html
    app/account/templates/account_create.html
    app/account/templates/account_edit.html
    app/account/templates/account_list.html
    app/account/templates/account_details.html
    app/workspace/templates/workspace_form.html
    app/workspace/templates/workspace_create.html
    app/workspace/templates/workspace_edit.html
    app/workspace/templates/workspace_list.html
    app/workspace/templates/workspace_details.html
    app/document/templates/document_upload.html
    app/document/templates/document_list.html
    app/document/templates/document_details.html
    app/workshop/templates/workshop_form.html
    app/workshop/templates/workshop_create.html
    app/workshop/templates/workshop_edit.html
    app/workshop/templates/workshop_list.html
    app/workshop/templates/workshop_details.html
    app/workshop/templates/workshop_lobby.html
    app/workshop/templates/workshop_room.html
    app/workshop/templates/workshop_report.html
    app/chat/templates/chat_room.html
    app/service/templates/service_agenda.html
    app/service/templates/service_plan.html
    app/service/templates/service_rules.html
    app/service/templates/service_tip.html
    app/service/templates/service_icebreaker.html
    app/service/templates/service_task.html
    app/service/templates/service_nudge.html
    app/service/templates/service_vote.html
    app/service/templates/service_idea.html
    app/service/templates/service_agent.html
)

# Create directories
for d in "${dirs[@]}"; do
    mkdir -p "$d"
done

# Create empty python & other files
for f in "${files[@]}"; do
    touch "$f"
done

# Create empty template files
for t in "${templates[@]}"; do
    touch "$t"
done

# Create sample instance files
# touch instance/app_database.sqlite
# touch instance/agent_memory.sqlite
touch instance/documents/_sample.png
touch instance/documents/_sample.pdf
touch instance/documents/_sample.docx
touch instance/documents/_sample.pptx
touch instance/documents/_sample.xlsx
touch instance/default/_profile.png

echo "Project skeleton created successfully!"
