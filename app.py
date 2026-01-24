import os
import uuid
import base64
import json
import time
from flask import Flask, request
from google.cloud import bigquery, secretmanager, tasks_v2
from google.protobuf import timestamp_pb2
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# -----------------------------
# Config
# -----------------------------
PROJECT_ID = os.environ["PROJECT_ID"]
BQ_DATASET = os.environ.get("BQ_DATASET", "automation")
USER_TABLE = os.environ.get("USER_TABLE", "user_admin")
RUN_TABLE = os.environ.get("RUN_TABLE", "run_items")
REGION = os.environ.get("REGION", "us-central1")
QUEUE_NAME = os.environ.get("TASK_QUEUE", "automation-queue")
TASK_HANDLER_URL = os.environ.get("TASK_HANDLER_URL","https://automation-27851544936.europe-west1.run.app")

bq_client = bigquery.Client()
secret_client = secretmanager.SecretManagerServiceClient()
tasks_client = tasks_v2.CloudTasksClient()

app = Flask(__name__)

# -----------------------------
# PUB/SUB endpoint
# -----------------------------
@app.route("/", methods=["POST"])
def pubsub_handler():
    print("🔥 Pub/Sub received")
    envelope = request.get_json()
    if not envelope or "message" not in envelope:
        return "Bad Request", 400

    pubsub_message = envelope["message"]
    data = pubsub_message.get("data")
    if not data:
        return "Bad Request: No data", 400

    input_json = json.loads(base64.b64decode(data).decode("utf-8"))
    run_id = str(uuid.uuid4())
    case_id = input_json["inputFormList"][0]["runId"]

    print(f"📥 Case {case_id} received. Enqueuing Cloud Task...")
    create_task(input_json, run_id, case_id)

    return "OK", 200

def create_task(input_json, run_id, case_id):
    parent = tasks_client.queue_path(PROJECT_ID, REGION, QUEUE_NAME)
    task_payload = {
        "input_json": input_json,
        "run_id": run_id,
        "case_id": case_id
    }
    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": TASK_HANDLER_URL,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(task_payload).encode()
        }
    }
    tasks_client.create_task(parent=parent, task=task)
    print(f"✅ Cloud Task created for Run ID {run_id}")

# -----------------------------
# Task handler endpoint
# -----------------------------
@app.route("/task_handler", methods=["POST"])
def task_handler():
    payload = request.get_json()
    input_json = payload["input_json"]
    run_id = payload["run_id"]
    case_id = payload["case_id"]

    print(f"🚀 Processing Run {run_id} for Case {case_id}")
    process_skipcvp(input_json, run_id, case_id)
    return "Task processed", 200

# -----------------------------
# Main processing
# -----------------------------
def process_skipcvp(input_json, run_id, case_id):
    user = None
    driver = None

    try:
        run_input = input_json["inputFormList"][0]
        form_fields = {f["placeHolder"]: f["value"] for f in run_input.get("formFields", [])}
        print(f"📄 Case ID: {case_id}, Form fields: {form_fields}")

        # Lock a user
        if not lock_user(run_id):
            msg = "⚠️ No available user"
            print(msg)
            update_run_item(run_id, "FAILED", msg)
            return

        user = get_locked_user(run_id)
        print(f"👤 Locked user: {user['username']}")

        # Get password
        password = get_password(user["secret_name"])

        # Selenium automation
        driver = login_to_cvp(user["username"], password)
        do_navigation(driver, form_fields)

        # Success
        update_run_item(run_id, "SUCCESS", "")
        print(f"✅ Run {run_id} completed successfully")

    except Exception as e:
        print(f"❌ Run {run_id} failed: {e}")
        update_run_item(run_id, "FAILED", str(e))

    finally:
        if user:
            release_user(run_id)
        if driver:
            driver.quit()

# -----------------------------
# BigQuery helpers
# -----------------------------
def lock_user(run_id):
    query = f"""
    UPDATE `{PROJECT_ID}.{BQ_DATASET}.{USER_TABLE}`
    SET run_id=@run_id, status='LOCKED', last_updated=CURRENT_TIMESTAMP()
    WHERE user_id = (
        SELECT user_id
        FROM `{PROJECT_ID}.{BQ_DATASET}.{USER_TABLE}`
        WHERE status='AVAILABLE' AND run_id IS NULL
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
    WHERE run_id=@run_id
    """
    rows = list(bq_client.query(query, job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("run_id", "STRING", run_id)]
    )).result())
    if not rows:
        return None
    return {"username": rows[0]["username"], "secret_name": rows[0]["secret_name"]}

def release_user(run_id):
    query = f"""
    UPDATE `{PROJECT_ID}.{BQ_DATASET}.{USER_TABLE}`
    SET run_id=NULL, status='AVAILABLE', last_updated=CURRENT_TIMESTAMP()
    WHERE run_id=@run_id
    """
    bq_client.query(query, job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("run_id", "STRING", run_id)]
    )).result()
    print(f"🔓 Released user for Run ID {run_id}")

def insert_run_item(run_id):
    query = f"INSERT INTO `{PROJECT_ID}.{BQ_DATASET}.{RUN_TABLE}` (run_id) VALUES (@run_id)"
    bq_client.query(query, job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("run_id", "STRING", run_id)]
    )).result()

def update_run_item(run_id, status, error_message):
    query = f"""
    UPDATE `{PROJECT_ID}.{BQ_DATASET}.{RUN_TABLE}`
    SET status=@status, error_message=@error_message, updated_at=CURRENT_TIMESTAMP()
    WHERE run_id=@run_id
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
# Selenium
# -----------------------------
def login_to_cvp(username, password):
    chrome_options = Options()
    chrome_options.binary_location = os.environ["CHROME_BIN"]
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")

    service = Service(os.environ["CHROMEDRIVER_PATH"])
    driver = webdriver.Chrome(service=service, options=chrome_options)

    driver.get("https://the-internet.herokuapp.com/login")
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.ID, "username")))
    driver.find_element(By.ID, "username").send_keys(username)
    driver.find_element(By.ID, "password").send_keys(password)
    driver.find_element(By.XPATH, '//*[@id="login"]/button').click()
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.XPATH, "//a[@href='/logout']")))

    print(f"✅ Logged in as {username}")
    return driver

def do_navigation(driver, form_fields):
    driver.get("https://the-internet.herokuapp.com/dropdown")
    WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.ID, "dropdown")))
    print("✅ Navigation complete")

# -----------------------------
# Flask entry
# -----------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

