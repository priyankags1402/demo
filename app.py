from flask import Flask, request, jsonify
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
import time

app = Flask(__name__)

@app.route("/", methods=["POST"])
def run_automation():
    payload = request.get_json()

    url = payload.get("url")
    steps = payload.get("xpaths", [])

    if not url or not steps:
        return jsonify({"status": "FAILED", "error": "url or xpaths missing"}), 400

    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    driver = webdriver.Chrome(options=options)

    execution_log = []

    try:
        driver.get(url)
        time.sleep(2)

        for
