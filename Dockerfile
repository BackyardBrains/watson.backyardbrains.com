FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py memory_client.py ./

ENV DATA_PATH=/data
ENV USE_MEMORY_API=true
ENV MEMORY_API_URL=https://memory.backyardbrains.com
EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
