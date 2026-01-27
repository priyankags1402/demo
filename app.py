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
# Run table helpers
# -----------------------------
def run_already_processed(case_id):
    query = f"""
    SELECT COUNT(*) AS cnt
    FROM `{PROJECT_ID}.{BQ_DATASET}.{RUN_TABLE}`
    WHERE case_id=@case_id AND status='SUCCESS'
    """
    rows = list(
        bq_client.query(
            query,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("case_id", "STRING", case_id)
                ]
            ),
        ).result()
    )
    return rows[0]["cnt"] > 0


def any_run_running():
    query = f"""
    SELECT COUNT(*) AS cnt
    FROM `{PROJECT_ID}.{BQ_DATASET}.{RUN_TABLE}`
    WHERE status='RUNNING'
    """
    rows = list(bq_client.query(query).result())
    return rows[0]["cnt"] > 0


def insert_run_item(run_id, case_id, status="RUNNING"):
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
    print(f" Run inserted | run_id={run_id}, case_id={case_id}")


def update_run_item(run_id, status, error_message=""):
    query = f"""
    UPDATE `{PROJECT_ID}.{BQ_DATASET}.{RUN_TABLE}`
    SET status=@status,
        error_message=@error_message,
        updated_at=CURRENT_TIMESTAMP()
    WHERE run_id=@run_id
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
    print(f" Run updated | run_id={run_id}, status={status}")


# -----------------------------
# User locking helpers
# -----------------------------
def lock_user(run_id):
    query = f"""
    UPDATE `{PROJECT_ID}.{BQ_DATASET}.{USER_TABLE}`
    SET run_id=@run_id,
        status='LOCKED',
        last_updated=CURRENT_TIMESTAMP()
    WHERE user_id = (
        SELECT user_id
        FROM `{PROJECT_ID}.{BQ_DATASET}.{USER_TABLE}`
        WHERE status='AVAILABLE'
          AND run_id IS NULL
        ORDER BY RAND()
        LIMIT 1
    )
    AND run_id IS NULL
    """
    job = bq_client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("run_id", "STRING", run_id)
            ]
        ),
    )
    job.result()
    return job.num_dml_affected_rows == 1


def get_locked_user(run_id):
    query = f"""
    SELECT username, secret_name
    FROM `{PROJECT_ID}.{BQ_DATASET}.{USER_TABLE}`
    WHERE run_id=@run_id
    """
    rows = list(
        bq_client.query(
            query,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("run_id", "STRING", run_id)
                ]
            ),
        ).result()
    )
    if not rows:
        return None
    return {
        "username": rows[0]["username"],
        "secret_name": rows[0]["secret_name"],
    }


def release_user(run_id):
    query = f"""
    UPDATE `{PROJECT_ID}.{BQ_DATASET}.{USER_TABLE}`
    SET run_id=NULL,
        status='AVAILABLE',
        last_updated=CURRENT_TIMESTAMP()
    WHERE run_id=@run_id
    """
    bq_client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("run_id", "STRING", run_id)
            ]
        ),
    ).result()
    print(f" User released | run_id={run_id}")


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
    options = Options()
    options.binary_location = os.environ.get("CHROME_BIN")

    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--remote-debugging-port=9222")

    service = Service(os.environ.get("CHROMEDRIVER_PATH"))
    driver = webdriver.Chrome(service=service, options=options)

    driver.get("https://the-internet.herokuapp.com/login")

    WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.ID, "username"))
    )

    driver.find_element(By.ID, "username").send_keys(username)
    driver.find_element(By.ID, "password").send_keys(password)
    driver.find_element(By.XPATH, '//*[@id="login"]/button').click()

    WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.XPATH, "//a[@href='/logout']"))
    )

    print(f" Logged in as {username}")
    return driver


def do_navigation(driver, form_fields):
    driver.get("https://the-internet.herokuapp.com/dropdown")
    time.sleep(2)
    print(f"Navigation done | fields={form_fields}")


# -----------------------------
# Main processing
# -----------------------------
def process_skipcvp(input_json, run_id, case_id):
    user = None
    driver = None

    try:
        run_input = input_json["inputFormList"][0]
        form_fields = {
            f["placeHolder"]: f["value"]
            for f in run_input.get("formFields", [])
        }

        print(f" Processing case_id={case_id}")

        # Lock an available user
        if not lock_user(run_id):
            raise Exception("No available user")

        user = get_locked_user(run_id)
        if not user:
            raise Exception("Locked user not found")

        password = get_password(user["secret_name"])

        # Login via Selenium
        driver = login_to_cvp(user["username"], password)
        do_navigation(driver, form_fields)

        # Mark the run as successful
        update_run_item(run_id, "SUCCESS")
        print(f" Case completed | case_id={case_id}")

    except Exception as e:
        print(f" Run failed | {e}")
        update_run_item(run_id, "FAILED", str(e))

    finally:
        # Attempt to log out if logged in
        if driver:
            try:
                logout_btn = driver.find_elements(By.XPATH, "//a[@href='/logout']")
                if logout_btn:
                    logout_btn[0].click()
                    print(" Logged out successfully")
            except Exception as e:
                print(f" Logout failed: {e}")
            finally:
                driver.quit()
                print(" Browser closed")

        # Release the user lock
        if user:
            release_user(run_id)
# -----------------------------
# Pub/Sub endpoint
# -----------------------------
@app.route("/", methods=["POST"])
def pubsub_handler():
    print(" Pub/Sub message received")

    envelope = request.get_json()
    if not envelope:
        return "Bad Request", 400

    message = envelope.get("message")
    if not message or "data" not in message:
        return "Bad Request", 400

    input_json = json.loads(
        base64.b64decode(message["data"]).decode("utf-8")
    )

    case_id = input_json["inputFormList"][0].get("runId", "UNKNOWN")

    # Idempotency
    if run_already_processed(case_id):
        print(f" Case already processed | {case_id}")
        return "OK", 200

    # Sequential processing
    if any_run_running():
        print(" Another run in progress")
        return "OK", 200

    run_id = str(uuid.uuid4())
    insert_run_item(run_id, case_id)

   
    process_skipcvp(input_json, run_id, case_id)

    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))


