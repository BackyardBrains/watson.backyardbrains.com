FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py memory_client.py background_queue.py capture_quality.py task_state.py watson_ideas.py ./
RUN chmod 0644 /app/*.py

ENV DATA_PATH=/data
ENV USE_MEMORY_API=true
ENV MEMORY_API_URL=https://memory.backyardbrains.com
EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
