from flask import Flask, jsonify
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

app = Flask(__name__)

@app.route("/", methods=["GET"])
def demo():
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    driver = webdriver.Chrome(options=options)

    try:
        driver.get("https://the-internet.herokuapp.com/login")

        driver.find_element(By.XPATH, '//*[@id="username"]').send_keys("tomsmith")
        driver.find_element(By.XPATH, '//*[@id="password"]').send_keys("SuperSecretPassword!")
        driver.find_element(By.XPATH, '//*[@id="login"]/button').click()

        message = driver.find_element(By.XPATH, '//*[@id="flash"]').text

        return jsonify({"status": "SUCCESS", "message": message})

    except Exception as e:
        return jsonify({"status": "ERROR", "error": str(e)}), 500

    finally:
        driver.quit()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)






