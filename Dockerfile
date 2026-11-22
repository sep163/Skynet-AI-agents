FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt requirements-voice.txt ./
ARG VOICE=1
RUN pip install --no-cache-dir -r requirements.txt && if [ "$VOICE" = "1" ]; then pip install --no-cache-dir -r requirements-voice.txt; fi
COPY . .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
