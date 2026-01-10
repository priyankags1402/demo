from flask import Flask, request, jsonify
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import time
import tempfile
import os

app = Flask(__name__)

# -------------------------------
# Selenium Setup
# -------------------------------
def create_driver():
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    return webdriver.Chrome(options=options)


# -------------------------------
# Task Implementations
# -------------------------------
def task_login(driver):
    driver.get("https://the-internet.herokuapp.com/login")
    driver.find_element(By.ID, "username").send_keys("tomsmith")
    driver.find_element(By.ID, "password").send_keys("SuperSecretPassword!")
    driver.find_element(By.XPATH, '//*[@id="login"]/button').click()
    return driver.find_element(By.ID, "flash").text.strip()


def task_logout(driver):
    task_login(driver)
    driver.find_element(By.XPATH, '//*[@href="/logout"]').click()
    return "Logged out successfully"


def task_checkbox(driver):
    driver.get("https://the-internet.herokuapp.com/checkboxes")
    checkbox = driver.find_elements(By.XPATH, "//input[@type='checkbox']")[0]
    checkbox.click()
    return "Checkbox clicked"


def task_dropdown(driver):
    driver.get("https://the-internet.herokuapp.com/dropdown")
    dropdown = Select(driver.find_element(By.ID, "dropdown"))
    dropdown.select_by_visible_text("Option 2")
    return "Dropdown Option 2 selected"


def task_iframe(driver):
    driver.get("https://the-internet.herokuapp.com/iframe")
    driver.switch_to.frame("mce_0_ifr")
    editor = driver.find_element(By.ID, "tinymce")
    editor.clear()
    editor.send_keys("Iframe text entered")
    driver.switch_to.default_content()
    return "Iframe text entered"


def task_hover(driver):
    driver.get("https://the-internet.herokuapp.com/hovers")
    figure = driver.find_element(By.CLASS_NAME, "figure")
    ActionChains(driver).move_to_element(figure).perform()
    return "Hover action performed"


def task_alert(driver):
    driver.get("https://the-internet.herokuapp.com/javascript_alerts")
    driver.find_element(By.XPATH, "//button[text()='Click for JS Alert']").click()
    alert = driver.switch_to.alert
    alert.accept()
    return "Alert accepted"


def task_new_window(driver):
    driver.get("https://the-internet.herokuapp.com/windows")
    main = driver.current_window_handle
    driver.find_element(By.LINK_TEXT, "Click Here").click()
    WebDriverWait(driver, 10).until(EC.number_of_windows_to_be(2))
    for h in driver.window_handles:
        if h != main:
            driver.switch_to.window(h)
    return f"Switched to window: {driver.title}"


def task_upload(driver):
    driver.get("https://the-internet.herokuapp.com/upload")
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.write(b"test upload")
    tmp.close()
    driver.find_element(By.ID, "file-upload").send_keys(tmp.name)
    driver.find_element(By.ID, "file-submit").click()
    os.unlink(tmp.name)
    return "File uploaded"


def task_dynamic_controls(driver):
    driver.get("https://the-internet.herokuapp.com/dynamic_controls")
    driver.find_element(By.XPATH, "//button[text()='Enable']").click()
    input_box = WebDriverWait(driver, 10).until(
        EC.element_to_be_clickable((By.XPATH, "//input"))
    )
    input_box.send_keys("Enabled input")
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

    driver = create_driver()

    try:
        result = TASKS[action](driver)
        return jsonify({
            "task_name": task_name,
            "action": action,
            "status": "SUCCESS",
            "result": result
        })

    except Exception as e:
        return jsonify({
            "task_name": task_name,
            "action": action,
            "status": "ERROR",
            "error": str(e)
        }), 500

    finally:
        driver.quit()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
