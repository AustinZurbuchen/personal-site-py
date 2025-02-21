FROM python:3.9-slim

WORKDIR /

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Make port 5000 available outside the container
EXPOSE 5000

# Run the Flask app with host=0.0.0.0 to make it accessible outside the container
CMD ["python", "-m", "server.py", "run", "--host=0.0.0.0"]