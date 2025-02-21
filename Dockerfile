FROM python:3.9-slim

COPY server.py /server.py

CMD ["python", "-u", "/server.py"]