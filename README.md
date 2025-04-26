# Brainstorm X

**Brainstorm X is an ai-embedded collaborative brainstorming platform designed to streamline workshop planning, executing and follow-up. It enables organizers to create structured workshops, manage participants and documents. It leverages the Granite 3.3 series of foundation models to generate workshop agenda, action plans, session rules, icebreakers, tips and even nudges participants to encourage engagement. The application has the ability to moderate voting and idea prioritization, access the feasibility of proposed idea and forecast the market trends**

## Key features
The solution performs the following operations:
1. 

## Architecture
The application is implemented with Python and JavaScript using Flask, LangChain libraries, IBM Granite 3.3 Models.

## Structure

```
app/
    __init__.py
    config.py
    models.py
    extensions.py
    sockets.py
    main/
        routes.py
        templates/
            base.html
            index.html
    workshop/
        routes.py
        templates/
            workshop_form.html
            workshop_create.html
            workshop_edit.html
            workshop_list.html
            workshop_details.html
            workshop_lobby.html
            workshop_room.html
            workshop_report.html
    agent/
        routes.py
        templates/
            assistant.html
    chat/
        routes.py
        templates/
            chat_room.html
    services/
        agenda.py
        tip.py

requirements.txt
run.py # <-- Entry point % python run.py
```