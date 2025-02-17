import os
from dotenv import main
from pymongo import MongoClient
from flask import Flask, request
from bson import json_util
from flask_cors import CORS
import json

main.load_dotenv()
db_user = os.getenv('DBUSER')
db_pass = os.getenv('DBPASS')
app = Flask(__name__)
CORS(app)

def get_database():
    CONNECTION_STRING = f"mongodb+srv://{db_user}:{db_pass}@personalsite.qbhpviu.mongodb.net/?retryWrites=true&w=majority&appName=PersonalSite"
    client = MongoClient(CONNECTION_STRING)
    return client['test']['resumes']

db = get_database()

def jsonify(text):
    return json.loads(json_util.dumps(text))

@app.route('/getResume', methods=['GET'])
def get_resume():
    print("GET /getResume endpoint hit")
    try:
        resume = db.find_one()
        if resume is None:
            return {"error": "No resume found in database"}, 404
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
def api_update_test():
    print("PUT /updateTest endpoint hit")
    print(request.get_json())
    try:
        data = request.get_json()
        if 'data' not in data:
            return {"error": "No test data provided"}, 400
        
        result = db.update_one({}, {'$set': {'test': data['data']}})

        if result.modified_count > 0:
            return {"status": "Success", "message": "Test updated successfully"}, 200
        else:
            return {"status": "Failed", "message": "No changes made to the database"}, 400
        
    except Exception as e:
        print(f"Update error: {str(e)}")
        return {"error": "Database connection failed"}, 500

if __name__ == '__main__':
    app.run(debug=True)