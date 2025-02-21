FROM python:3.9-slim

COPY server.py /server.py

RUN pip install python-dotenv
RUN pip install pymongo
RUN pip install flask
RUN pip install flask-cors

CMD ["python", "-u", "/server.py"]