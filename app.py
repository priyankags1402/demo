import os
import uuid
import time
import base64
import json
from flask import Flask, request
from google.cloud import bigquery, secretmanager
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager

# -----------------------------
# Config / clients
# -----------------------------
PROJECT_ID = os.environ.get("PROJECT_ID", "wired-coda-483805-b3")
BQ_DATASET = os.environ.get("BQ_DATASET", "automation")
BQ_TABLE = os.environ.get("BQ_TABLE", "user_admin")

bq_client = bigquery.Client()
secret_client = secretmanager.SecretManagerServiceClient()

app = Flask(__name__)

# -----------------------------
# Cloud Run / Eventarc endpoint
# -----------------------------
@app.route("/", methods=["POST"])
def pubsub_handler():
    print("🔥 Handler entered")

    # Print raw request data
    print("📨 Raw request data:", request.data)

    try:
        envelope = request.get_json()
        print("📨 Parsed JSON envelope:", envelope)

        if not envelope:
            print("❌ No JSON payload received")
            return "Bad Request: No JSON", 400

        pubsub_message = envelope.get("message", {})
        if "data" not in pubsub_message:
            print("❌ No data in message")
            return "Bad Request: Missing data", 400

        input_json = json.loads(base64.b64decode(pubsub_message["data"]).decode("utf-8"))
        print("📥 Received input JSON:", input_json)

        process_skipcvp(input_json)

        print("✅ Returning OK to Pub/Sub")
        return "OK", 200

    except Exception as e:
        print("❌ Error handling message:", str(e))
        return f"Internal Error: {e}", 500

# -----------------------------
# Main Processing Logic
# -----------------------------
def process_skipcvp(input_json):
    print("🔹 Processing skipCVP task")
    run_id_internal = str(uuid.uuid4())
    user = None
    driver = None
    timestamp_seq = int(time.time() * 1000)

    # Extract caseId, tcId, form fields
    try:
        run_input = input_json["inputFormList"][0]
        case_id = run_input["runId"]
        tc_id = run_input.get("tcId", "24533")
        form_fields = {f["placeHolder"]: f["value"] for f in run_input.get("formFields", [])}
        print(f"📄 Case ID: {case_id}, TC ID: {tc_id}, Form fields: {form_fields}")
    except Exception as e:
        print(f"❌ Invalid input JSON: {e}")
        return

    try:
        # STEP 1: Lock user
        print("🔹 Attempting to lock a user...")
        if not lock_user(run_id_internal):
            print("⚠️ No available user")
            return
        print("✅ User locked")

        # STEP 2: Get locked user
        user = get_locked_user(run_id_internal)
        if not user:
            print("❌ No locked user found")
            return
        print(f"👤 Locked user: {user['username']}")

        username = user["username"]
        password = get_password(user["secret_name"])
        print("🔑 Password retrieved from Secret Manager")

        # STEP 3: Login to CVP (headless)
        print(f"🌐 Logging in to CVP as {username}")
        driver = login_to_cvp(username, password)
        print("✅ Login successful")

        # STEP 4: Do automation
        print("🔹 Starting CVP navigation")
        do_navigation(driver, form_fields)
        print(f"✅ Run {case_id} completed successfully")

    except Exception as e:
        print(f"❌ Run {case_id} failed: {e}")

    finally:
        # Always release user and quit browser
        if user:
            print("🔄 Releasing user")
            release_user(run_id_internal)
        if driver:
            print("🚪 Quitting browser")
            driver.quit()

# -----------------------------
# BigQuery Functions
# -----------------------------
def lock_user(run_id):
    query = f"""
    UPDATE `{PROJECT_ID}.{BQ_DATASET}.{BQ_TABLE}`
    SET run_id = @run_id,
        status = 'LOCKED',
        last_updated = CURRENT_TIMESTAMP()
    WHERE user_id = (
        SELECT user_id
        FROM `{PROJECT_ID}.{BQ_DATASET}.{BQ_TABLE}`
        WHERE status = 'AVAILABLE'
          AND run_id IS NULL
        ORDER BY RAND()
        LIMIT 1
    )
    AND run_id IS NULL
    """
    print("📝 Executing BigQuery lock_user query")
    job = bq_client.query(query, job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("run_id", "STRING", run_id)]
    ))
    job.result()
    print(f"🔹 Rows affected by lock_user: {job.num_dml_affected_rows}")
    return job.num_dml_affected_rows == 1

def get_locked_user(run_id):
    print("📝 Querying BigQuery for locked user")
    query = f"""
    SELECT username, secret_name
    FROM `{PROJECT_ID}.{BQ_DATASET}.{BQ_TABLE}`
    WHERE run_id = @run_id
    """
    job = bq_client.query(query, job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("run_id", "STRING", run_id)]
    ))
    rows = list(job.result())
    if not rows:
        print("⚠️ No rows found in get_locked_user")
        return None
    return {"username": rows[0]["username"], "secret_name": rows[0]["secret_name"]}

def release_user(run_id):
    print("📝 Releasing user in BigQuery")
    query = f"""
    UPDATE `{PROJECT_ID}.{BQ_DATASET}.{BQ_TABLE}`
    SET run_id = NULL,
        status = 'AVAILABLE',
        last_updated = CURRENT_TIMESTAMP()
    WHERE run_id = @run_id
    """
    bq_client.query(query, job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("run_id", "STRING", run_id)]
    )).result()
    print("✅ User released")

# -----------------------------
# Secret Manager
# -----------------------------
def get_password(secret_name):
    print(f"🔐 Retrieving password from Secret Manager: {secret_name}")
    secret_path = f"projects/{PROJECT_ID}/secrets/{secret_name}/versions/latest"
    response = secret_client.access_secret_version(name=secret_path)
    return response.payload.data.decode("UTF-8")

# -----------------------------
# Selenium Automation
# -----------------------------
def login_to_cvp(username, password):
    chrome_options = Options()
    chrome_options.binary_location = os.environ.get("CHROME_BIN")

    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")

    service = Service(os.environ.get("CHROMEDRIVER_PATH"))
    driver = webdriver.Chrome(service=service, options=chrome_options)

    try:
        print("🌐 Opening login page")
        driver.get("https://the-internet.herokuapp.com/login")
        time.sleep(2)

        print("⌨️ Entering credentials")
        driver.find_element(By.ID, "username").send_keys(username)
        driver.find_element(By.ID, "password").send_keys(password)
        driver.find_element(By.XPATH, '//*[@id="login"]/button').click()

        time.sleep(3)
        #if "dashboard" not in driver.current_url:
            #raise Exception("Login failed")
        print("✅ Login success")
        return driver

    except Exception as e:
        print(f"❌ Login failed: {e}")
        driver.quit()
        raise e

def do_navigation(driver, form_fields):
    print("🧭 Navigating CVP site with input:", form_fields)
    driver.get("https://the-internet.herokuapp.com/dropdown")
    time.sleep(2)
    #start_button = driver.find_element(By.ID, "startProcess")
    #start_button.click()
    #time.sleep(2)
    print("✅ Navigation complete")

# -----------------------------
# Flask entry point for Cloud Run
# -----------------------------
if __name__ == "__main__":
    print("🚀 Starting Flask app")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))




