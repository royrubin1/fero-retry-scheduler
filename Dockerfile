FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The database lives on a volume shared by the API and the workers.
RUN useradd --create-home app && mkdir /data && chown app /data
USER app
ENV DB_PATH=/data/jobs.db API_HOST=0.0.0.0 PYTHONUNBUFFERED=1

COPY app app
COPY demo demo

EXPOSE 8000
CMD ["python", "-m", "app.api"]
