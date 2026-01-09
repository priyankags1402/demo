from flask import Flask, request, jsonify
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

app = Flask(__name__)

@app.route("/", methods=["POST"])
def automation():
    """
    Expects a JSON payload like:
    {
        "url": "https://the-internet.herokuapp.com/login",
        "actions": [
            {"type": "send_keys", "by": "id", "locator": "username", "value": "tomsmith"},
            {"type": "send_keys", "by": "id", "locator": "password", "value": "SuperSecretPassword!"},
            {"type": "click", "by": "xpath", "locator": '//*[@id="login"]/button'}
        ],
        "get_text": {"by": "id", "locator": "flash"}
    }
    """
    data = request.get_json()

    if not data or "url" not in data:
        return jsonify({"status": "ERROR", "error": "Missing URL in request"}), 400

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")

    driver = webdriver.Chrome(options=options)

    try:
        driver.get(data["url"])

        # Perform actions
        for action in data.get("actions", []):
            by = getattr(By, action["by"].upper())
            element = driver.find_element(by, action["locator"])
            if action["type"] == "send_keys":
                element.send_keys(action["value"])
            elif action["type"] == "click":
                element.click()

        # Extract text if requested
        get_text = data.get("get_text")
        text_result = None
        if get_text:
            by = getattr(By, get_text["by"].upper())
            text_result = driver.find_element(by, get_text["locator"]).text

        return jsonify({"status": "SUCCESS", "message": text_result})

    except Exception as e:
        return jsonify({"status": "ERROR", "error": str(e)}), 500

    finally:
        driver.quit()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
