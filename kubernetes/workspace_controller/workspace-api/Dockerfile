FROM python:3.9-slim

WORKDIR /app

# Install necessary packages
RUN apt-get update && apt-get install -y curl git && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Run the application
CMD ["gunicorn", "--bind", "0.0.0.0:3000", "wsgi:app"]