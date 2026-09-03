import os
import hmac
from functools import wraps
from dotenv import main
from pymongo import MongoClient
from flask import Flask, request
from bson import json_util
from flask_cors import CORS
from utils import sort_work_items
import json

main.load_dotenv()
db_user = os.getenv('DBUSER')
db_pass = os.getenv('DBPASS')
admin_token = (os.getenv('ADMIN_TOKEN') or '').strip()
app = Flask(__name__)
CORS(app, origins=["http://localhost:3000", "http://127.0.0.1:3000"], supports_credentials=True)

def get_database():
    CONNECTION_STRING = f"mongodb+srv://{db_user}:{db_pass}@personalsite.qbhpviu.mongodb.net/?retryWrites=true&w=majority&appName=PersonalSite"
    client = MongoClient(CONNECTION_STRING)
    return client['test']

db = get_database()['resumes']
admin_db = get_database()['admins']

def jsonify(text):
    return json.loads(json_util.dumps(text))

def require_token(handler):
    """Gate a route behind the ADMIN_TOKEN bearer token.

    Fails closed: if ADMIN_TOKEN is unset the route returns 503 rather than
    running, so a missing env var can never silently reopen a write endpoint.
    """
    @wraps(handler)
    def wrapper(*args, **kwargs):
        if not admin_token:
            print("ADMIN_TOKEN is not set - refusing write request")
            return {"error": "Server is not configured for writes"}, 503

        header = request.headers.get('Authorization', '')
        if not header.startswith('Bearer '):
            return {"error": "Unauthorized"}, 401

        presented = header[len('Bearer '):].strip()
        if not hmac.compare_digest(presented, admin_token):
            return {"error": "Unauthorized"}, 401

        return handler(*args, **kwargs)
    return wrapper

@app.route('/getResume', methods=['GET'])
def get_resume():
    print("GET /getResume endpoint hit")
    try:
        resume = db.find_one()
        if resume is None:
            return {"error": "No resume found in database"}, 404
        resume['experiences']['work'] = sort_work_items(resume['experiences']['work'])
        return jsonify(resume)
    except Exception as e:
        print(f"Database error: {str(e)}")
        return {"error": "Database connection failed"}, 500
    
@app.route('/getAbilities', methods=['GET'])
def api_get_abilites():
    print("GET /getAbilities endpoint hit")
    response = jsonify(db.find_one()['abilities'])
    return response

@app.route('/getExperiences', methods=['GET'])
def api_get_experiences():
    print("GET /getExperiences endpoint hit")
    response = jsonify(db.find_one()['experiences'])
    return response

@app.route('/getLinks', methods=['GET'])
def api_get_links():
    print("GET /getLinks endpoint hit")
    response = jsonify(db.find_one()['links'])
    return response

@app.route('/getProfile', methods=['GET'])
def api_get_profile():
    print("GET /getProfile endpoint hit")
    response = jsonify(db.find_one()['profile'])
    return response

@app.route('/getQuotes', methods=['GET'])
def api_get_quotes():
    print("GET /getQuotes endpoint hit")
    response = jsonify(db.find_one()['quotes'])
    return response

@app.route('/updateTest', methods=['PUT'])
@require_token
def api_update_test():
    print("PUT /updateTest endpoint hit")
    try:
        data = request.get_json(silent=True) or {}
        value = data.get('data')
        if not isinstance(value, str):
            return {"error": "Field 'data' is required and must be a string"}, 400

        resume = db.find_one({}, {'_id': 1})
        if resume is None:
            return {"error": "No resume found in database"}, 404

        result = db.update_one({'_id': resume['_id']}, {'$set': {'test': value}})
        if result.matched_count == 0:
            return {"error": "No resume found in database"}, 404

        return {"status": "Success", "message": "Test updated successfully"}, 200

    except Exception as e:
        print(f"Update error: {str(e)}")
        return {"error": "Database connection failed"}, 500

@app.route('/login', methods=['POST'])
def api_login():
    print("POST /login endpoint hit")
    data = request.get_json()
    if 'username' not in data or 'password' not in data:
        return {"error": "Username and password are required"}, 400
    admin = admin_db.find_one({'username': data['username']})
    if admin is None:
        return {"error": "Invalid username or password"}, 401
    if admin['password'] != data['password']:
        return {"error": "Invalid username or password"}, 401
    return {"status": "Success", "message": "Login successful"}, 200

if __name__ == '__main__':
    app.run(debug=True)