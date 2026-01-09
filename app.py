from flask import Flask, request, jsonify
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

app = Flask(__name__)

# Map actions to Selenium tasks
def perform_action(action, driver):
    try:
        if action == "login":
            driver.get("https://the-internet.herokuapp.com/login")
            driver.find_element(By.ID, "username").send_keys("tomsmith")
            driver.find_element(By.ID, "password").send_keys("SuperSecretPassword!")
            driver.find_element(By.XPATH, '//*[@id="login"]/button').click()
            message = driver.find_element(By.ID, "flash").text
            return {"status": "SUCCESS", "message": message.strip()}

        elif action == "check_status":
            driver.get("https://the-internet.herokuapp.com/status_codes")
            codes = [el.text for el in driver.find_elements(By.TAG_NAME, "li")]
            return {"status": "SUCCESS", "codes": codes}

        else:
            return {"status": "ERROR", "message": f"Unknown action: {action}"}

    except Exception as e:
        return {"status": "ERROR", "message": str(e)}


@app.route("/", methods=["POST"])
def run_task():
    data = request.get_json()
    task_name = data.get("task_name", "unnamed_task")
    action = data.get("action")

    # Selenium Chrome options
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    driver = webdriver.Chrome(options=options)
    try:
        result = perform_action(action, driver)
        result["task_name"] = task_name
        return jsonify(result)

    finally:
        driver.quit()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
