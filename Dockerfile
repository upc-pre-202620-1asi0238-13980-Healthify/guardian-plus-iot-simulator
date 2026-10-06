FROM python:3.14-slim
WORKDIR /app

# Logs straight to `docker logs`, without buffering
ENV PYTHONUNBUFFERED=1

# Cache dependencies separately from sources
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY simulator simulator

RUN useradd --system app
USER app

EXPOSE 5000

CMD ["python", "simulator/cli.py", "serve"]
