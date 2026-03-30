#!/usr/bin/env python3
"""SBTET Attendance API Server

A Flask API that fetches attendance data from SBTET Telangana and provides CORS-enabled endpoints.
"""
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import os
import time
import re

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

DEFAULT_URL_TEMPLATE = "https://www.sbtet.telangana.gov.in/api/api/PreExamination/getAttendanceReport?Pin={pin}"

# Results proxy (HTML)
RESULTS_URL_TEMPLATE = "http://18.61.7.125/result/{pin}"

# Results (JSON)
RESULTS_JSON_URL_TEMPLATE = "https://www.sbtet.telangana.gov.in/api/api/Results/GetConsolidatedResults?Pin={pin}"

# Simple in-memory cache to reduce repeated upstream calls during demos.
# { pin_lower: (timestamp, html_text) }
_RESULTS_CACHE = {}
_RESULTS_CACHE_TTL_SECONDS = 5 * 60

# { pin_lower: (timestamp, json_dict) }
_RESULTS_JSON_CACHE = {}
_RESULTS_JSON_CACHE_TTL_SECONDS = 5 * 60


def fetch_report_pin(pin: str):
    """Fetch attendance report from SBTET API"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko)"
            " Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.sbtet.telangana.gov.in/",
    }
    
    # Try with double /api/ first, then fallback to single /api/
    urls_to_try = [
        DEFAULT_URL_TEMPLATE.format(pin=pin),
        DEFAULT_URL_TEMPLATE.replace("/api/api/", "/api/").format(pin=pin)
    ]
    
    last_exc = None
    for url in urls_to_try:
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            
            # SBTET API returns a JSON string, so we need to parse it
            try:
                # First try direct JSON parsing
                data = resp.json()
                # If data is a string, parse it again
                if isinstance(data, str):
                    import json
                    data = json.loads(data)
                return data
            except ValueError as json_err:
                # If that fails, try parsing the text response
                try:
                    import json
                    data = json.loads(resp.text)
                    return data
                except Exception as e:
                    print(f"DEBUG - Failed to parse JSON. Error: {e}")
                    print(f"DEBUG - Response text (first 500 chars): {resp.text[:500]}")
                    raise json_err
                    
        except requests.exceptions.HTTPError as exc:
            last_exc = exc
            continue
        except ValueError as exc:
            raise exc
    
    # If we get here, none of the URLs succeeded
    if last_exc:
        raise last_exc
    raise requests.exceptions.RequestException("Unknown error fetching from SBTET API")


def _to_number(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    # keep digits and dot only
    cleaned = re.sub(r"[^0-9.]", "", s)
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except Exception:
        return None


def _pick_number_by_key(obj: dict, patterns: list[str]):
    if not isinstance(obj, dict):
        return None
    for key, value in obj.items():
        k = str(key)
        if any(re.search(p, k, flags=re.IGNORECASE) for p in patterns):
            n = _to_number(value)
            if n is not None:
                return n
    return None


def _compute_attendance_summary(student_info: dict, records: list[dict]):
    # Prefer student-level fields; if absent, fall back to the first record.
    source = student_info if isinstance(student_info, dict) and student_info else None
    if source is None and isinstance(records, list) and records:
        if isinstance(records[0], dict):
            source = records[0]

    total_days = _pick_number_by_key(source or {}, [r"total.*day", r"working.*day", r"no.*day", r"totday", r"twd"])
    present_days = _pick_number_by_key(source or {}, [r"present.*day", r"attend.*day", r"presentday", r"pday"]) 
    percent = _pick_number_by_key(source or {}, [r"percent", r"percentage", r"attend.*%", r"att.*per"])

    # If percentage is missing but total/present exist, compute.
    if percent is None and total_days is not None and total_days > 0 and present_days is not None:
        percent = (present_days / total_days) * 100.0

    # Normalize totals to integers when they look like whole numbers.
    def _as_int_if_whole(n):
        if n is None:
            return None
        if abs(n - round(n)) < 1e-9:
            return int(round(n))
        return n

    total_days_n = _as_int_if_whole(total_days)
    present_days_n = _as_int_if_whole(present_days)
    absent_days_n = None
    if isinstance(total_days_n, (int, float)) and isinstance(present_days_n, (int, float)):
        absent_days_n = _as_int_if_whole(float(total_days_n) - float(present_days_n))

    return {
        "attendancePercentage": None if percent is None else round(float(percent), 2),
        "totalDays": total_days_n,
        "presentDays": present_days_n,
        "absentDays": absent_days_n,
    }


def _results_headers():
    return {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko)"
            " Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    }


def fetch_results_json(pin: str):
    """Fetch consolidated results (JSON) from the official SBTET API."""
    pin_key = (pin or "").strip().lower()
    if not pin_key:
        raise ValueError("Missing pin")

    now = time.time()
    cached = _RESULTS_JSON_CACHE.get(pin_key)
    if cached:
        cached_at, cached_json = cached
        if now - cached_at < _RESULTS_JSON_CACHE_TTL_SECONDS:
            return cached_json

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko)"
            " Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.sbtet.telangana.gov.in/",
    }

    urls_to_try = [
        RESULTS_JSON_URL_TEMPLATE.format(pin=pin_key),
        RESULTS_JSON_URL_TEMPLATE.replace("/api/api/", "/api/").format(pin=pin_key),
    ]

    last_exc = None
    for url in urls_to_try:
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, str):
                import json

                data = json.loads(data)
            if not data:
                raise requests.exceptions.RequestException("Upstream returned empty JSON")

            _RESULTS_JSON_CACHE[pin_key] = (now, data)
            if len(_RESULTS_JSON_CACHE) > 200:
                for k, (ts, _) in list(_RESULTS_JSON_CACHE.items()):
                    if now - ts > _RESULTS_JSON_CACHE_TTL_SECONDS:
                        _RESULTS_JSON_CACHE.pop(k, None)

            return data
        except requests.exceptions.HTTPError as exc:
            last_exc = exc
            continue
        except ValueError as exc:
            # bad JSON
            last_exc = exc
            continue

    if last_exc:
        raise last_exc
    raise requests.exceptions.RequestException("Unknown error fetching consolidated results")


@app.route("/api/results", methods=["GET"])
def get_results_json():
    """Proxy the consolidated results JSON by PIN."""
    pin = request.args.get("pin")
    if not pin:
        return jsonify({"success": False, "error": "Missing pin parameter"}), 400

    try:
        data = fetch_results_json(pin)
        return jsonify({"success": True, "pin": pin, "data": data}), 200
    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code if hasattr(e, 'response') else 502
        if status_code == 404:
            return jsonify({"success": False, "error": "Student not found. Please check the PIN."}), 404
        return jsonify({"success": False, "error": f"HTTP Error: {str(e)}"}), status_code
    except requests.exceptions.Timeout:
        return jsonify({"success": False, "error": "Request timeout. Please try again."}), 504
    except Exception as e:
        return jsonify({"success": False, "error": f"Server error: {str(e)}"}), 500


def fetch_results_html(pin: str) -> str:
    pin_key = (pin or "").strip().lower()
    if not pin_key:
        raise ValueError("Missing pin")

    now = time.time()
    cached = _RESULTS_CACHE.get(pin_key)
    if cached:
        cached_at, cached_html = cached
        if now - cached_at < _RESULTS_CACHE_TTL_SECONDS:
            return cached_html

    url = RESULTS_URL_TEMPLATE.format(pin=pin_key)
    resp = requests.get(url, headers=_results_headers(), timeout=20)
    if resp.status_code == 404:
        raise requests.exceptions.HTTPError("Student not found", response=resp)
    resp.raise_for_status()

    html = resp.text or ""
    if len(html) < 200:
        raise requests.exceptions.RequestException("Upstream returned empty HTML")

    _RESULTS_CACHE[pin_key] = (now, html)
    # opportunistic cache cleanup
    if len(_RESULTS_CACHE) > 200:
        for k, (ts, _) in list(_RESULTS_CACHE.items()):
            if now - ts > _RESULTS_CACHE_TTL_SECONDS:
                _RESULTS_CACHE.pop(k, None)

    return html


@app.route("/api/results/raw", methods=["GET"])
def get_results_raw():
    """Proxy the results HTML page by PIN.

    Frontend parses the HTML into structured analytics.
    """
    pin = request.args.get("pin")
    if not pin:
        return jsonify({"success": False, "error": "Missing pin parameter"}), 400

    try:
        html = fetch_results_html(pin)
        return jsonify({"success": True, "pin": pin, "html": html}), 200
    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code if hasattr(e, 'response') else 502
        if status_code == 404:
            return jsonify({"success": False, "error": "Student not found. Please check the PIN."}), 404
        return jsonify({"success": False, "error": f"HTTP Error: {str(e)}"}), status_code
    except requests.exceptions.Timeout:
        return jsonify({"success": False, "error": "Request timeout. Please try again."}), 504
    except Exception as e:
        return jsonify({"success": False, "error": f"Server error: {str(e)}"}), 500


@app.route("/api/attendance", methods=["GET"])
def get_attendance():
    """API endpoint to fetch attendance by PIN"""
    pin = request.args.get("pin")
    
    if not pin:
        return jsonify({"error": "Missing pin parameter"}), 400

    try:
        data = fetch_report_pin(pin)
        
        # Debug: Log the raw response
        print(f"DEBUG - Raw API response for PIN {pin}:")
        print(f"Response type: {type(data)}")
        
        if not data:
            return jsonify({
                "success": False,
                "error": "No data returned from SBTET API"
            }), 404
        
        # Extract student info and attendance records
        response = {
            "success": True,
            "studentInfo": {},
            "attendanceRecords": [],
            "attendanceSummary": {
                "attendancePercentage": None,
                "totalDays": None,
                "presentDays": None,
                "absentDays": None,
            },
        }
        
        # SBTET returns Table (student info) and Table1 (daily attendance)
        if isinstance(data, dict):
            # Check if response indicates no data/invalid PIN
            if not data or all(not v for v in data.values()):
                print(f"DEBUG - Empty or null response from SBTET")
                return jsonify({
                    "success": False,
                    "error": "No data found for this PIN. Please verify the PIN is correct."
                }), 404
            
            if "Table" in data and isinstance(data["Table"], list) and data["Table"]:
                response["studentInfo"] = data["Table"][0]
                print(f"DEBUG - Student info extracted: {response['studentInfo']}")
            else:
                print(f"DEBUG - No Table found or Table is empty")
            
            if "Table1" in data and isinstance(data["Table1"], list):
                response["attendanceRecords"] = data["Table1"]
                print(f"DEBUG - Found {len(response['attendanceRecords'])} attendance records in Table1")
            elif "Table" in data and isinstance(data["Table"], list):
                # Only use Table for records if we haven't already used it for student info
                if not response["studentInfo"]:
                    response["attendanceRecords"] = data["Table"]
                    print(f"DEBUG - Found {len(response['attendanceRecords'])} attendance records in Table")
            else:
                print(f"DEBUG - No attendance records found")

        # Compute summary fields for UI (without requiring the full table).
        try:
            response["attendanceSummary"] = _compute_attendance_summary(
                response.get("studentInfo") or {},
                response.get("attendanceRecords") or [],
            )
        except Exception as e:
            print(f"DEBUG - Failed to compute attendance summary: {e}")
        
        # Final check: if no student info, return error
        if not response["studentInfo"] or len(response["studentInfo"]) == 0:
            print(f"DEBUG - No student info in final response")
            return jsonify({
                "success": False,
                "error": "No data found for this PIN. The PIN may be invalid or not in the SBTET system."
            }), 404
        
        print(f"DEBUG - Final response structure: {response.keys()}")
        print(f"DEBUG - Student info has {len(response['studentInfo'])} fields")
        print(f"DEBUG - Attendance records: {len(response['attendanceRecords'])} records")
        return jsonify(response), 200
        
    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code if hasattr(e, 'response') else 502
        if status_code == 404:
            return jsonify({"error": "Student not found. Please check the PIN."}), 404
        return jsonify({"error": f"HTTP Error: {str(e)}"}), status_code
        
    except requests.exceptions.Timeout:
        return jsonify({"error": "Request timeout. Please try again."}), 504
        
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Network error: {str(e)}"}), 502
        
    except ValueError as e:
        error_msg = f"Invalid JSON response: {str(e)}"
        print(f"ERROR - {error_msg}")
        return jsonify({"success": False, "error": error_msg}), 502
        
    except Exception as e:
        error_msg = f"Server error: {str(e)}"
        print(f"ERROR - {error_msg}")
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": error_msg}), 500


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint"""
    return jsonify({"status": "ok", "service": "SBTET Attendance API"}), 200

