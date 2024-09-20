import os
from dotenv import load_dotenv
from pymongo import MongoClient
from flask import Flask, request
from bson import json_util
import json

load_dotenv()
db_user = os.getenv('DBUSER')
db_pass = os.getenv('DBPASS')
app = Flask(__name__)

def get_database():
    CONNECTION_STRING = f"mongodb+srv://{db_user}:{db_pass}@personalsite.qbhpviu.mongodb.net/?retryWrites=true&w=majority&appName=PersonalSite"
    client = MongoClient(CONNECTION_STRING)
    return client['test']['resumes']

db = get_database()

def jsonify(text):
    return json.loads(json_util.dumps(text))

@app.route('/getResume', methods=['GET', 'POST'])
def get_resume():
    if(request.method != 'POST'):
        resume = jsonify(db.find_one())
        return resume
    else:
        return {"status": "Failed"}
    
@app.route('/getAbilities', methods=['GET'])
def api_get_abilites():
    response = jsonify(db.find_one()['abilities'])
    return response

@app.route('/getExperiences', methods=['GET'])
def api_get_experiences():
    response = jsonify(db.find_one()['experiences'])
    return response

@app.route('/getLinks', methods=['GET'])
def api_get_links():
    response = jsonify(db.find_one()['links'])
    return response

@app.route('/getProfile', methods=['GET'])
def api_get_profile():
    response = jsonify(db.find_one()['profile'])
    return response

@app.route('/getQuotes', methods=['GET'])
def api_get_quotes():
    response = jsonify(db.find_one()['quotes'])
    return response