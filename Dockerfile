FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --prefer-binary -r requirements.txt

COPY . .

CMD gunicorn main:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120
