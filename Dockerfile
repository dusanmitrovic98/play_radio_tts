# Use an official Python runtime as the base image
FROM python:3.11.11-slim

# Install FFmpeg and other dependencies
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Install Poetry
RUN pip install poetry==1.7.1

# Set the working directory
WORKDIR /usr/src/app

# Copy Poetry files
COPY pyproject.toml poetry.lock ./

# Install Python dependencies with Poetry
RUN poetry config virtualenvs.create false && poetry install --no-dev

# Copy the rest of the application code
COPY . .

# Expose the port Render expects
EXPOSE 5002

# Command to run your application
CMD ["python", "main.py"]