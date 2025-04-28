# Use an official Python runtime as a parent image
FROM python:3.10-slim

# Set the working directory inside the container
WORKDIR /app

# Copy requirements.txt and install dependencies
COPY requirements.txt .
# Install dependencies
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Expose the Flask port
EXPOSE 5001

# Run Flask with Gunicorn
# CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:5000", "app:app"]

# Default command to run application with Python.
CMD ["python", "-u", "run.py"]

