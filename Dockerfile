FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir \
    fastapi \
    "uvicorn[standard]" \
    requests \
    boto3 \
    Pillow \
    pydantic \
    pyproj \
    rasterio \
    pyshp \
    shapely \
    ezdxf \
    "psycopg[binary]"

COPY . .

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000}"]
