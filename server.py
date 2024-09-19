import os
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
db_user = os.getenv('DBUSER')
db_pass = os.getenv('DBPASS')

def get_database():
    CONNECTION_STRING = f"mongodb+srv://{db_user}:{db_pass}@personalsite.qbhpviu.mongodb.net/?retryWrites=true&w=majority&appName=PersonalSite"
    client = MongoClient(CONNECTION_STRING)
    return client['test']

dbname = get_database()
collection_name = dbname['resumes']

items = collection_name.find()
for item in items:
    print(item['profile'])