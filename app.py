import os
import uuid
import time
import base64
import json
from threading import Thread
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
# Helper: Check if run exists
# -----------------------------
def run_exists(run_id):
    query = f"""
    SELECT COUNT(*) as cnt
    FROM `{PROJECT_ID}.{BQ_DATASET}.{RUN_TABLE}`
    WHERE run_id=@run_id
    """
    rows = list(bq_client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("run_id", "STRING", run_id)]
        )
    ).result())
    return rows[0]["cnt"] > 0

# -----------------------------
# Insert run item
# -----------------------------
def insert_run_item(run_id):
    if run_exists(run_id):
        print(f"⚠️ Run {run_id} already exists. Skipping insert.")
        return
    query = f"""
    INSERT INTO `{PROJECT_ID}.{BQ_DATASET}.{RUN_TABLE}` (run_id)
    VALUES (@run_id)
    """
    bq_client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("run_id", "STRING", run_id)]
        )
    ).result()
    print(f"✅ Inserted Run ID {run_id} into {RUN_TABLE}")

# -----------------------------
# Update run status
# -----------------------------
def update_run_item(run_id, status, error_message=""):
    query = f"""
    UPDATE `{PROJECT_ID}.{BQ_DATASET}.{RUN_TABLE}`
    SET status=@status, error_message=@error_message, created_at=CURRENT_TIMESTAMP()
    WHERE run_id=@run_id
    """
    bq_client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("run_id", "STRING", run_id),
                bigquery.ScalarQueryParameter("status", "STRING", status),
                bigquery.ScalarQueryParameter("error_message", "STRING", error_message)
            ]
        )
    ).result()
    print(f"✅ Updated Run ID {run_id} status to {status}")

# -----------------------------
# Lock / unlock user
# -----------------------------
def lock_user(run_id):
    query = f"""
    UPDATE `{PROJECT_ID}.{BQ_DATASET}.{USER_TABLE}`
    SET run_id = @run_id, status = 'LOCKED', last_updated = CURRENT_TIMESTAMP()
    WHERE user_id = (
        SELECT user_id
        FROM `{PROJECT_ID}.{BQ_DATASET}.{USER_TABLE}`
        WHERE status='AVAILABLE' AND run_id IS NULL
        ORDER BY RAND()
        LIMIT 1
    ) AND run_id IS NULL
    """
    job = bq_client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("run_id", "STRING", run_id)]
        )
    )
    job.result()
    return job.num_dml_affected_rows == 1

def get_locked_user(run_id):
    query = f"""
    SELECT username, secret_name
    FROM `{PROJECT_ID}.{BQ_DATASET}.{USER_TABLE}`
    WHERE run_id = @run_id
    """
    rows = list(bq_client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("run_id", "STRING", run_id)]
        )
    ).result())
    if not rows:
        return None
    return {"username": rows[0]["username"], "secret_name": rows[0]["secret_name"]}

def release_user(run_id):
    query = f"""
    UPDATE `{PROJECT_ID}.{BQ_DATASET}.{USER_TABLE}`
    SET run_id=NULL, status='AVAILABLE', last_updated=CURRENT_TIMESTAMP()
    WHERE run_id=@run_id
    """
    bq_client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("run_id", "STRING", run_id)]
        )
    ).result()
    print(f"🔓 Released user for Run ID {run_id}")

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
        print(f"✅ Logged in as {username}")
        return driver
    except Exception as e:
        driver.quit()
        raise e

def do_navigation(driver, form_fields):
    driver.get("https://the-internet.herokuapp.com/dropdown")
    time.sleep(2)
    print(f"✅ Navigation complete with fields: {form_fields}")

# -----------------------------
# Main processing logic
# -----------------------------
def process_skipcvp(input_json, run_id):
    user = None
    driver = None

    try:
        run_input = input_json["inputFormList"][0]
        case_id = run_input.get("runId", "UNKNOWN")
        form_fields = {f["placeHolder"]: f["value"] for f in run_input.get("formFields", [])}
        print(f"📄 Case ID: {case_id}, Form fields: {form_fields}")

        if not lock_user(run_id):
            msg = "⚠️ No available user"
            print(msg)
            update_run_item(run_id, "FAILED", msg)
            return

        user = get_locked_user(run_id)
        if not user:
            msg = "❌ Locked user not found"
            print(msg)
            update_run_item(run_id, "FAILED", msg)
            return

        print(f"👤 Locked user: {user['username']}")
        password = get_password(user["secret_name"])
        print("🔑 Password retrieved")

        driver = login_to_cvp(user["username"], password)
        do_navigation(driver, form_fields)

        update_run_item(run_id, "SUCCESS", "")
        print(f"✅ Run {case_id} completed successfully")

    except Exception as e:
        print(f"❌ Run failed: {e}")
        update_run_item(run_id, "FAILED", str(e))

    finally:
        if user:
            release_user(run_id)
        if driver:
            driver.quit()
            print("🚪 Browser quit")

# -----------------------------
# Flask entry point
# -----------------------------
@app.route("/", methods=["POST"])
def pubsub_handler():
    print("🔥 Pub/Sub message received")
    try:
        envelope = request.get_json()
        if not envelope:
            return "Bad Request: Empty body", 400

        pubsub_message = envelope.get("message")
        if not pubsub_message or "data" not in pubsub_message:
            return "Bad Request: Missing Pub/Sub data", 400

        input_json = json.loads(base64.b64decode(pubsub_message["data"]).decode("utf-8"))
        run_id = input_json["inputFormList"][0].get("runId", str(uuid.uuid4()))

        # Insert run in BQ
        insert_run_item(run_id)

        # Process asynchronously in a thread
        Thread(target=process_skipcvp, args=(input_json, run_id)).start()

        print("✅ Returning 200 OK to Pub/Sub")
        return "OK", 200

    except Exception as e:
        print(f"❌ Error processing Pub/Sub: {e}")
        return f"Internal Error: {e}", 500

if __name__ == "__main__":
    print("🚀 Starting Flask app")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
