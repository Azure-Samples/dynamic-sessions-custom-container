# Azure Container Apps Dynamic Sessions - Custom Container
# This container provides a secure, isolated environment for code execution

FROM python:3.11-slim

# Install minimal system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install essential Python packages (lightweight selection with matplotlib)
RUN pip install --no-cache-dir \
    numpy \
    pandas \
    openpyxl \
    xlrd \
    pyarrow \
    lxml \
    matplotlib \
    requests \
    flask \
    gunicorn

# Create working directory
WORKDIR /workspace

# Copy the session server code
COPY server.py /workspace/server.py

# Expose port 8080
EXPOSE 8080

# Start gunicorn directly (no shell wrapper)
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--timeout", "120", "--log-level", "info", "server:app"]