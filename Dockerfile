FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY mediasort.py .

VOLUME ["/source", "/movies", "/tv"]

ENV TMDB_API_KEY=""
ENV TMDB_READ_TOKEN=""

# Default: web UI on port 8080
EXPOSE 8080

ENTRYPOINT ["python3", "mediasort.py"]
CMD ["--web", "--host", "0.0.0.0", "--port", "8080", "/source", "--movies", "/movies", "--tv", "/tv"]
