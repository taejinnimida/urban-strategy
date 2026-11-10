FROM python:3.12-slim
WORKDIR /srv/survey
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["uvicorn", "dev_app:app", "--host", "0.0.0.0", "--port", "8000"]
