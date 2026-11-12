FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir \
    fastapi \
    "uvicorn[standard]" \
    requests \
    pydantic \
    pyproj \
    pyshp \
    shapely

COPY . .

CMD ["sh", "-c", "uvicorn landscape_app:app --host 0.0.0.0 --port ${PORT:-10000}"]
