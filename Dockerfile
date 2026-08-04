FROM node:24-alpine AS frontend

WORKDIR /frontend

COPY package.json package-lock.json ./
RUN npm ci

COPY app/static/markdown.js app/static/markdown.js
COPY tests/markdown-renderer.test.mjs tests/markdown-renderer.test.mjs
RUN npm run test:markdown
RUN mkdir -p /assets && \
    cp node_modules/marked/lib/marked.umd.js /assets/marked.umd.js && \
    cp node_modules/dompurify/dist/purify.min.js /assets/purify.min.js

FROM python:3.12-slim

WORKDIR /app

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .
COPY --from=frontend /assets/ ./static/vendor/

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
