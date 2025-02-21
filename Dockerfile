FROM python:3.9-slim-buster

WORKDIR /

COPY server.py .

CMD ["python", "server.py"]