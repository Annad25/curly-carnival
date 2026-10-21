FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# optional reasoning layer: the claude CLI (uses CLAUDE_KEY at runtime)
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm \
    && npm install -g @anthropic-ai/claude-code && apt-get clean && rm -rf /var/lib/apt/lists/*
COPY . .
RUN useradd -m checker && chown -R checker /app
USER checker
ENTRYPOINT ["python", "src/main.py"]
CMD ["check", "--config", "sites.yaml"]
