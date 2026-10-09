FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY static ./static
RUN mkdir -p /home/data
ENV APP_DB_PATH=/home/data/assistant.sqlite3 PORT=8000
EXPOSE 8000
CMD ["gunicorn", "--workers", "1", "--threads", "8", "--timeout", "120", "--bind", "0.0.0.0:8000", "app:app"]
