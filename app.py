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
# Config
# -----------------------------
PROJECT_ID = os.environ.get("PROJECT_ID", "wired-coda-483805-b3")
BQ_DATASET = os.environ.get("BQ_DATASET", "automation")
USER_TABLE = os.environ.get("USER_TABLE", "user_admin")
RUN_TABLE = os.environ.get("RUN_TABLE", "run_items")

bq_client = bigquery.Client()
secret_client = secretmanager.SecretManagerServiceClient()

app = Flask(__name__)

# -----------------------------
# Pub/Sub Handler
# -----------------------------
@app.route("/", methods=["POST"])
def pubsub_handler():
    print("🚀 Received Pub/Sub message")
    envelope = request.get_json()
    if not envelope:
        print("❌ Bad request: empty envelope")
        return "Bad Request", 400

    pubsub_message = envelope.get("message", {})
    if "data" not in pubsub_message:
        print("❌ Bad request: missing data")
        return "Bad Request", 400

    input_json = json.loads(
        base64.b64decode(pubsub_message["data"]).decode("utf-8")
    )
    print("📥 Decoded input JSON:", input_json)

    run_input = input_json["inputFormList"][0]
    case_id = run_input["runId"]
    print(f"🔹 Case ID: {case_id}")

    # 🔒 CASE IDEMPOTENCY
    if case_already_processed(case_id):
        print(f"🔁 Case {case_id} already processed — skipping")
        return "OK", 200

    run_id = str(uuid.uuid4())
    insert_run_item(run_id, case_id, "RUNNING")
    print(f"🆔 Run ID {run_id} inserted into {RUN_TABLE} with status RUNNING")

    # ✅ ACK PUB/SUB IMMEDIATELY
    response = ("OK", 200)

    # 🚀 Do work AFTER ACK
    process_skipcvp(input_json, run_id, case_id)

    return response

# -----------------------------
# Main Processing
# -----------------------------
def process_skipcvp(input_json, run_id, case_id):
    print(f"🔹 Starting processing for Case ID {case_id}, Run ID {run_id}")
    user = None
    driver = None

    try:
        run_input = input_json["inputFormList"][0]
        form_fields = {f["placeHolder"]: f["value"] for f in run_input.get("formFields", [])}
        print("📄 Form fields to fill:", form_fields)

        print("🔒 Attempting to lock a user")
        if not lock_user(run_id):
            raise Exception("No available user")
        print("✅ User locked successfully")

        user = get_locked_user(run_id)
        print(f"👤 Locked user: {user['username']}")

        password = get_password(user["secret_name"])
        print("🔑 Password retrieved from Secret Manager")

        print(f"🌐 Logging in to CVP as {user['username']}")
        driver = login_to_cvp(user["username"], password)
        print("✅ Login successful")

        print("🔹 Performing navigation / automation")
        do_navigation(driver, form_fields)
        print(f"✅ Case {case_id} processed successfully")

        update_run_item(run_id, "SUCCESS", "")
        print(f"📝 Updated run status to SUCCESS for Run ID {run_id}")

    except Exception as e:
        print(f"❌ Run {run_id} failed: {e}")
        update_run_item(run_id, "FAILED", str(e))

    finally:
        if driver:
            print("🚪 Quitting browser")
            driver.quit()
            del driver
        if user:
            print("🔄 Releasing user")
            release_user(run_id)

# -----------------------------
# BigQuery Helpers
# -----------------------------
def case_already_processed(case_id):
    query = f"""
    SELECT 1
    FROM `{PROJECT_ID}.{BQ_DATASET}.{RUN_TABLE}`
    WHERE case_id = @case_id
      AND status IN ('RUNNING', 'SUCCESS')
    LIMIT 1
    """
    job = bq_client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("case_id", "STRING", case_id)
            ]
        )
    )
    processed = any(job.result())
    print(f"🔍 Case {case_id} already processed? {processed}")
    return processed

def insert_run_item(run_id, case_id, status):
    query = f"""
    INSERT INTO `{PROJECT_ID}.{BQ_DATASET}.{RUN_TABLE}`
    (run_id, case_id, status, created_at)
    VALUES (@run_id, @case_id, @status, CURRENT_TIMESTAMP())
    """
    bq_client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("run_id", "STRING", run_id),
                bigquery.ScalarQueryParameter("case_id", "STRING", case_id),
                bigquery.ScalarQueryParameter("status", "STRING", status),
            ]
        ),
    ).result()
    print(f"💾 Inserted Run ID {run_id} for Case {case_id} with status {status}")

def update_run_item(run_id, status, error_message):
    query = f"""
    UPDATE `{PROJECT_ID}.{BQ_DATASET}.{RUN_TABLE}`
    SET status = @status,
        error_message = @error_message,
        updated_at = CURRENT_TIMESTAMP()
    WHERE run_id = @run_id
    """
    bq_client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("run_id", "STRING", run_id),
                bigquery.ScalarQueryParameter("status", "STRING", status),
                bigquery.ScalarQueryParameter("error_message", "STRING", error_message),
            ]
        ),
    ).result()
    print(f"💾 Updated Run ID {run_id} to status {status}")

def lock_user(run_id):
    query = f"""
    UPDATE `{PROJECT_ID}.{BQ_DATASET}.{USER_TABLE}`
    SET run_id = @run_id, status = 'LOCKED'
    WHERE user_id = (
        SELECT user_id
        FROM `{PROJECT_ID}.{BQ_DATASET}.{USER_TABLE}`
        WHERE status = 'AVAILABLE' AND run_id IS NULL
        LIMIT 1
    )
    """
    job = bq_client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("run_id", "STRING", run_id)]
        ),
    )
    job.result()
    print(f"🔒 Attempted to lock a user for Run ID {run_id}")
    return job.num_dml_affected_rows == 1

def get_locked_user(run_id):
    query = f"""
    SELECT username, secret_name
    FROM `{PROJECT_ID}.{BQ_DATASET}.{USER_TABLE}`
    WHERE run_id = @run_id
    """
    rows = list(
        bq_client.query(
            query,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("run_id", "STRING", run_id)]
            ),
        ).result()
    )
    return rows[0]

def release_user(run_id):
    query = f"""
    UPDATE `{PROJECT_ID}.{BQ_DATASET}.{USER_TABLE}`
    SET run_id = NULL, status = 'AVAILABLE'
    WHERE run_id = @run_id
    """
    bq_client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("run_id", "STRING", run_id)]
        ),
    ).result()
    print(f"🔓 Released user for Run ID {run_id}")

# -----------------------------
# Secret Manager
# -----------------------------
def get_password(secret_name):
    path = f"projects/{PROJECT_ID}/secrets/{secret_name}/versions/latest"
    password = secret_client.access_secret_version(name=path).payload.data.decode("UTF-8")
    print(f"🔑 Retrieved secret for {secret_name}")
    return password

# -----------------------------
# Selenium
# -----------------------------
def login_to_cvp(username, password):
    print(f"🌐 Logging in as {username}")
    options = Options()
    options.binary_location = os.environ["CHROME_BIN"]
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")

    service = Service(os.environ["CHROMEDRIVER_PATH"])
    driver = webdriver.Chrome(service=service, options=options)

    driver.get("https://the-internet.herokuapp.com/login")
    driver.find_element(By.ID, "username").send_keys(username)
    driver.find_element(By.ID, "password").send_keys(password)
    driver.find_element(By.XPATH, '//*[@id="login"]/button').click()

    WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.XPATH, "//a[@href='/logout']"))
    )
    print("✅ Login successful")
    return driver

def do_navigation(driver, form_fields):
    print("🔹 Performing navigation / form automation")
    driver.get("https://the-internet.herokuapp.com/dropdown")
    WebDriverWait(driver, 5).until(
        EC.presence_of_element_located((By.ID, "dropdown"))
    )
    print("✅ Navigation complete")

# -----------------------------
# Entry
# -----------------------------
if __name__ == "__main__":
    print("🚀 Starting Flask app")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
