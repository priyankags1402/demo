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
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# -----------------------------
# Config / clients
# -----------------------------
PROJECT_ID = os.environ.get("PROJECT_ID", "wired-coda-483805-b3")
BQ_DATASET = os.environ.get("BQ_DATASET", "automation")
USER_TABLE = os.environ.get("USER_TABLE", "user_admin")
RUN_TABLE = os.environ.get("RUN_TABLE", "run_items")

bq_client = bigquery.Client()
secret_client = secretmanager.SecretManagerServiceClient()

app = Flask(__name__)

# -----------------------------
# Cloud Run / Eventarc endpoint
# -----------------------------
@app.route("/", methods=["POST"])
def pubsub_handler():
    print("🔥 Handler entered")
    print("📨 Raw request data:", request.data)

    try:
        envelope = request.get_json()
        print("📨 Parsed JSON envelope:", envelope)
        if not envelope:
            return "Bad Request: No JSON", 400

        pubsub_message = envelope.get("message", {})
        if "data" not in pubsub_message:
            return "Bad Request: Missing data", 400

        input_json = json.loads(base64.b64decode(pubsub_message["data"]).decode("utf-8"))
        print("📥 Received input JSON:", input_json)

        run_id = str(uuid.uuid4())
        insert_run_item(run_id)

        process_skipcvp(input_json, run_id)

        print("✅ Returning OK to Pub/Sub")
        return "OK", 200

    except Exception as e:
        print("❌ Error handling message:", str(e))
        return f"Internal Error: {e}", 500

# -----------------------------
# Main Processing Logic
# -----------------------------
def process_skipcvp(input_json, run_id):
    print("🔹 Processing skipCVP task")
    user = None
    driver = None

    try:
        run_input = input_json["inputFormList"][0]
        case_id = run_input["runId"]
        tc_id = run_input.get("tcId", "24533")
        form_fields = {f["placeHolder"]: f["value"] for f in run_input.get("formFields", [])}
        print(f"📄 Case ID: {case_id}, TC ID: {tc_id}, Form fields: {form_fields}")
    except Exception as e:
        print(f"❌ Invalid input JSON: {e}")
        update_run_item(run_id, "FAILED", str(e))
        return

    try:
        print("🔹 Attempting to lock a user...")
        if not lock_user(run_id):
            msg = "⚠️ No available user"
            print(msg)
            update_run_item(run_id, "FAILED", msg)
            return
        print("✅ User locked")

        user = get_locked_user(run_id)
        if not user:
            msg = "❌ No locked user found"
            print(msg)
            update_run_item(run_id, "FAILED", msg)
            return
        print(f"👤 Locked user: {user['username']}")

        password = get_password(user["secret_name"])
        print("🔑 Password retrieved from Secret Manager")

        print(f"🌐 Logging in to CVP as {user['username']}")
        driver = login_to_cvp(user["username"], password)
        print("✅ Login successful")

        print("🔹 Starting CVP navigation")
        do_navigation(driver, form_fields)

        print(f"✅ Run {case_id} completed successfully")
        update_run_item(run_id, "SUCCESS", "")

    except Exception as e:
        print(f"❌ Run {case_id} failed: {e}")
        update_run_item(run_id, "FAILED", str(e))

    finally:
        if user:
            print("🔄 Releasing user")
            release_user(run_id)
        if driver:
            print("🚪 Quitting browser")
            driver.quit()

# -----------------------------
# BigQuery Functions
# -----------------------------
def lock_user(run_id):
    query = f"""
    UPDATE `{PROJECT_ID}.{BQ_DATASET}.{USER_TABLE}`
    SET run_id = @run_id, status = 'LOCKED', last_updated = CURRENT_TIMESTAMP()
    WHERE user_id = (
        SELECT user_id
        FROM `{PROJECT_ID}.{BQ_DATASET}.{USER_TABLE}`
        WHERE status = 'AVAILABLE' AND run_id IS NULL
        ORDER BY RAND()
        LIMIT 1
    ) AND run_id IS NULL
    """
    job = bq_client.query(query, job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("run_id", "STRING", run_id)]
    ))
    job.result()
    return job.num_dml_affected_rows == 1

def get_locked_user(run_id):
    query = f"""
    SELECT username, secret_name
    FROM `{PROJECT_ID}.{BQ_DATASET}.{USER_TABLE}`
    WHERE run_id = @run_id
    """
    job = bq_client.query(query, job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("run_id", "STRING", run_id)]
    ))
    rows = list(job.result())
    if not rows:
        return None
    return {"username": rows[0]["username"], "secret_name": rows[0]["secret_name"]}

def release_user(run_id):
    query = f"""
    UPDATE `{PROJECT_ID}.{BQ_DATASET}.{USER_TABLE}`
    SET run_id = NULL, status = 'AVAILABLE', last_updated = CURRENT_TIMESTAMP()
    WHERE run_id = @run_id
    """
    bq_client.query(query, job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("run_id", "STRING", run_id)]
    )).result()

def insert_run_item(run_id):
    query = f"""
    INSERT INTO `{PROJECT_ID}.{BQ_DATASET}.{RUN_TABLE}` (run_id)
    VALUES (@run_id)
    """
    bq_client.query(query, job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("run_id", "STRING", run_id)]
    )).result()

def update_run_item(run_id, status, error_message):
    query = f"""
    UPDATE `{PROJECT_ID}.{BQ_DATASET}.{RUN_TABLE}`
    SET status = @status, error_message = @error_message, created_at = CURRENT_TIMESTAMP()
    WHERE run_id = @run_id
    """
    bq_client.query(query, job_config=bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("run_id", "STRING", run_id),
            bigquery.ScalarQueryParameter("status", "STRING", status),
            bigquery.ScalarQueryParameter("error_message", "STRING", error_message)
        ]
    )).result()

# -----------------------------
# Secret Manager
# -----------------------------
def get_password(secret_name):
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
        driver.get("https://the-internet.herokuapp.com/login")
        time.sleep(2)
        driver.find_element(By.ID, "username").send_keys(username)
        driver.find_element(By.ID, "password").send_keys(password)
        driver.find_element(By.XPATH, '//*[@id="login"]/button').click()
        time.sleep(3)
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.XPATH, "//a[@href='/logout']"))
        )

        print("✅ Login successful")
        return driver
    except Exception as e:
        driver.quit()
        raise e

def do_navigation(driver, form_fields):
    driver.get("https://the-internet.herokuapp.com/dropdown")
    time.sleep(2)
    print("✅ Navigation complete")

# -----------------------------
# Flask entry point
# -----------------------------
if __name__ == "__main__":
    print("🚀 Starting Flask app")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
