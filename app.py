from flask import Flask, request, jsonify
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from google.cloud import storage
import tempfile
import uuid
import os
import time

app = Flask(__name__)

# -------------------------------
# GCS CONFIG
# -------------------------------
GCS_BUCKET = "wired-coda-483805-b3"
GCS_PREFIX = "screenshots"

# -------------------------------
# Screenshot + GCS Helpers
# -------------------------------
def upload_to_gcs(local_path, gcs_path):
    client = storage.Client()
    bucket = client.bucket(GCS_BUCKET)
    blob = bucket.blob(gcs_path)
    blob.upload_from_filename(local_path)
    return f"gs://{GCS_BUCKET}/{gcs_path}"

def snap(driver, label, ctx):
    ctx["step"] += 1
    filename = f"{ctx['step']:02d}_{label}.png"
    local_path = os.path.join(ctx["dir"], filename)
    driver.save_screenshot(local_path)

    gcs_path = f"{GCS_PREFIX}/{ctx['run_id']}/{filename}"
    gcs_uri = upload_to_gcs(local_path, gcs_path)

    ctx["screenshots"].append(gcs_uri)

# -------------------------------
# Selenium Setup
# -------------------------------
def create_driver():
    options = Options()
    options.binary_location = "/usr/bin/chromium"
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")

    service = Service("/usr/bin/chromedriver")
    return webdriver.Chrome(service=service, options=options)

# -------------------------------
# Task Implementations
# -------------------------------
def task_login(driver, ctx):
    driver.get("https://the-internet.herokuapp.com/login")
    snap(driver, "login_page", ctx)

    driver.find_element(By.ID, "username").send_keys("tomsmith")
    driver.find_element(By.ID, "password").send_keys("SuperSecretPassword!")
    snap(driver, "credentials_entered", ctx)

    driver.find_element(By.XPATH, '//*[@id="login"]/button').click()
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.ID, "flash")))
    snap(driver, "after_login", ctx)

    return driver.find_element(By.ID, "flash").text.strip()

def task_logout(driver, ctx):
    task_login(driver, ctx)
    driver.find_element(By.XPATH, '//*[@href="/logout"]').click()
    snap(driver, "after_logout", ctx)
    return "Logged out successfully"

def task_checkbox(driver, ctx):
    driver.get("https://the-internet.herokuapp.com/checkboxes")
    snap(driver, "checkbox_page", ctx)

    checkbox = driver.find_elements(By.XPATH, "//input[@type='checkbox']")[0]
    checkbox.click()
    snap(driver, "checkbox_clicked", ctx)

    return "Checkbox clicked"

def task_dropdown(driver, ctx):
    driver.get("https://the-internet.herokuapp.com/dropdown")
    snap(driver, "dropdown_page", ctx)

    Select(driver.find_element(By.ID, "dropdown")).select_by_visible_text("Option 2")
    snap(driver, "dropdown_selected", ctx)

    return "Dropdown Option 2 selected"

def task_iframe(driver, ctx):
    driver.get("https://the-internet.herokuapp.com/iframe")
    snap(driver, "iframe_page", ctx)

    driver.switch_to.frame("mce_0_ifr")
    editor = driver.find_element(By.ID, "tinymce")
    editor.clear()
    editor.send_keys("Iframe text entered")
    snap(driver, "iframe_text_entered", ctx)

    driver.switch_to.default_content()
    return "Iframe text entered"

def task_hover(driver, ctx):
    driver.get("https://the-internet.herokuapp.com/hovers")
    snap(driver, "hover_page", ctx)

    figure = driver.find_element(By.CLASS_NAME, "figure")
    ActionChains(driver).move_to_element(figure).perform()
    snap(driver, "hover_performed", ctx)

    return "Hover action performed"

def task_alert(driver, ctx):
    driver.get("https://the-internet.herokuapp.com/javascript_alerts")
    snap(driver, "alert_page", ctx)

    driver.find_element(By.XPATH, "//button[text()='Click for JS Alert']").click()
    snap(driver, "alert_shown", ctx)

    driver.switch_to.alert.accept()
    snap(driver, "alert_accepted", ctx)

    return "Alert accepted"

def task_new_window(driver, ctx):
    driver.get("https://the-internet.herokuapp.com/windows")
    snap(driver, "windows_page", ctx)

    main = driver.current_window_handle
    driver.find_element(By.LINK_TEXT, "Click Here").click()

    WebDriverWait(driver, 10).until(EC.number_of_windows_to_be(2))
    for h in driver.window_handles:
        if h != main:
            driver.switch_to.window(h)

    snap(driver, "new_window_opened", ctx)
    return f"Switched to window: {driver.title}"

def task_upload(driver, ctx):
    driver.get("https://the-internet.herokuapp.com/upload")
    snap(driver, "upload_page", ctx)

    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.write(b"test upload")
    tmp.close()

    driver.find_element(By.ID, "file-upload").send_keys(tmp.name)
    driver.find_element(By.ID, "file-submit").click()
    snap(driver, "file_uploaded", ctx)

    os.unlink(tmp.name)
    return "File uploaded"

def task_dynamic_controls(driver, ctx):
    driver.get("https://the-internet.herokuapp.com/dynamic_controls")
    snap(driver, "dynamic_controls_page", ctx)

    driver.find_element(By.XPATH, "//button[text()='Enable']").click()
    input_box = WebDriverWait(driver, 10).until(
        EC.element_to_be_clickable((By.XPATH, "//input"))
    )
    input_box.send_keys("Enabled input")
    snap(driver, "dynamic_input_enabled", ctx)

    return "Dynamic input enabled and text entered"

# -------------------------------
# Task Router
# -------------------------------
TASKS = {
    "login": task_login,
    "logout": task_logout,
    "checkbox": task_checkbox,
    "dropdown": task_dropdown,
    "iframe": task_iframe,
    "hover": task_hover,
    "alert": task_alert,
    "new_window": task_new_window,
    "upload": task_upload,
    "dynamic_controls": task_dynamic_controls,
}

# -------------------------------
# API Endpoint
# -------------------------------
@app.route("/", methods=["POST"])
def run_task():
    data = request.get_json()
    task_name = data.get("task_name", "unnamed_task")
    action = data.get("action")

    if action not in TASKS:
        return jsonify({
            "task_name": task_name,
            "status": "ERROR",
            "message": f"Unsupported action: {action}"
        }), 400

    run_id = str(uuid.uuid4())
    screenshot_dir = f"/tmp/screenshots/{run_id}"
    os.makedirs(screenshot_dir, exist_ok=True)

    ctx = {
        "run_id": run_id,
        "dir": screenshot_dir,
        "step": 0,
        "screenshots": []
    }

    driver = create_driver()

    try:
        result = TASKS[action](driver, ctx)
        return jsonify({
            "task_name": task_name,
            "action": action,
            "status": "SUCCESS",
            "result": result,
            "screenshots": ctx["screenshots"]
        })

    except Exception as e:
        snap(driver, "error", ctx)
        return jsonify({
            "task_name": task_name,
            "action": action,
            "status": "ERROR",
            "error": str(e),
            "screenshots": ctx["screenshots"]
        }), 500

    finally:
        driver.quit()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
