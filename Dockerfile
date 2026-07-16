FROM node:20-alpine AS assets

WORKDIR /build

COPY package*.json ./
RUN npm ci

COPY tailwind.config.js ./
COPY app/ ./app/
RUN npx tailwindcss -i ./app/static/src.css -o /assets/app.css --minify \
    && sed -i 's@/\*! tailwindcss[^*]*\*/@@g' /assets/app.css

FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY --from=assets /assets/app.css ./app/static/app.css

EXPOSE 8000

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "app.main:app"]
