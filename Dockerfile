FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY mediasort.py .

VOLUME ["/source", "/movies", "/tv"]

ENV TMDB_API_KEY=""
ENV TMDB_READ_TOKEN=""

ENTRYPOINT ["python3", "mediasort.py"]
CMD ["/source", "--movies", "/movies", "--tv", "/tv", "-x", "--watch", "300", "--cleanup"]
