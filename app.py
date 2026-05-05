import os
import razorpay
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv
import hmac
import hashlib
import requests
from werkzeug.middleware.proxy_fix import ProxyFix

# Load environment variables
load_dotenv()

app = Flask(__name__)

# Trust X-Forwarded-* headers from proxies (Vercel, Load Balancers)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Allow CORS
allowed_origins_env = os.getenv('ALLOWED_ORIGINS')
if allowed_origins_env:
    allowed_origins = allowed_origins_env.split(',')
else:
    # Default to allowing local development if no origins specified
    allowed_origins = ["http://localhost:5173", "http://localhost:3000"]

CORS(app, origins=allowed_origins, supports_credentials=True)

# Rate Limiter setup
redis_url = os.getenv('REDIS_URL')
storage_uri = redis_url if redis_url else "memory://"

if not redis_url and os.getenv('FLASK_ENV') != 'development':
    app.logger.warning("Using memory storage for rate limiting in production. This is NOT recommended.")

limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["300 per day", "100 per hour"],
    storage_uri=storage_uri,
)

# Razorpay Client Initialization
RAZORPAY_KEY_ID = os.getenv('RAZORPAY_KEY_ID')
RAZORPAY_KEY_SECRET = os.getenv('RAZORPAY_KEY_SECRET')

if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
    app.logger.warning("Razorpay credentials not found in environment!")

client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

@app.route('/', methods=['GET'])
def home():
    return jsonify({"status": "Saga Razorpay Backend is Running"}), 200

@app.route('/create-order', methods=['POST'])
@limiter.limit("20 per minute")
def create_order():
    """
    Creates a Razorpay order. 
    Amount should be in the smallest currency unit (e.g., paise for INR).
    """
    try:
        data = request.get_json()
        amount = data.get('amount') # In rupees
        currency = data.get('currency', 'INR')

        if not amount:
            return jsonify({"error": "Amount is required"}), 400

        # Create Order
        # amount * 100 because Razorpay expects amount in paise
        order_data = {
            'amount': int(amount) * 100,
            'currency': currency,
            'payment_capture': 1 # Auto capture payment
        }

        order = client.order.create(data=order_data)
        return jsonify(order), 200

    except Exception as e:
        app.logger.error(f"Order Creation Error: {str(e)}")
        return jsonify({"error": "Failed to create payment order"}), 500

@app.route('/verify-payment', methods=['POST'])
@limiter.limit("10 per minute")
def verify_payment():
    """
    Verifies the payment signature sent by Razorpay after a successful payment.
    """
    try:
        data = request.get_json()
        
        razorpay_order_id = data.get('razorpay_order_id')
        razorpay_payment_id = data.get('razorpay_payment_id')
        razorpay_signature = data.get('razorpay_signature')

        # Construct the signature verification string
        # pattern: order_id + "|" + payment_id
        msg = f"{razorpay_order_id}|{razorpay_payment_id}"
        
        # Verify signature using HMAC SHA256
        generated_signature = hmac.new(
            RAZORPAY_KEY_SECRET.encode(),
            msg.encode(),
            hashlib.sha256
        ).hexdigest()

        if generated_signature == razorpay_signature:
            return jsonify({"status": "success", "message": "Payment verified successfully"}), 200
        else:
            return jsonify({"status": "failure", "message": "Signature verification failed"}), 400

    except Exception as e:
        app.logger.error(f"Payment Verification Error: {str(e)}")
        return jsonify({"error": "Payment verification system error"}), 500

@app.route('/track-order', methods=['POST'])
@limiter.limit("20 per minute")
def track_order():
    """
    Handles order tracking with DTDC API.
    """
    try:
        data = request.get_json()
        awb_number = data.get('awbNumber')
        
        if not awb_number:
            return jsonify({"error": "AWB number is required"}), 400

        # DTDC credentials from environment variables
        username = os.getenv('DTDC_USERNAME', 'YOUR_DTDC_USERNAME')
        password = os.getenv('DTDC_PASSWORD', 'YOUR_DTDC_PASSWORD')

        # Phase 1: Authentication
        auth_url = f"https://blktracksvc.dtdc.com/dtdc-api/api/dtdc/authenticate?username={username}&password={password}"
        auth_response = requests.get(auth_url)
        token = auth_response.text

        if not token or "Error" in token:
             return jsonify({"error": "Failed to authenticate with DTDC. Check credentials."}), 500

        # Phase 2: Get Tracking Details
        tracking_url = 'https://blktracksvc.dtdc.com/dtdc-api/rest/JSONCnTrk/getTrackDetails'
        payload = {
            'trkType': 'cnno',
            'strcnno': awb_number,
            'addtnlDtl': 'Y'
        }
        headers = {
            'X-Access-Token': token,
            'Content-Type': 'application/json'
        }
        
        tracking_response = requests.post(tracking_url, json=payload, headers=headers)
        dtdc_data = tracking_response.json()

        # Check if DTDC returned a valid response
        if not dtdc_data or 'details' not in dtdc_data:
            return jsonify({
                "error": "Tracking information is not yet available for this AWB."
            }), 404

        details = dtdc_data.get('details', [])
        latest = details[0] if details else {}

        return jsonify({
            "status": latest.get('scanStatus', 'In Transit'),
            "location": latest.get('scanLocation', 'Processing Hub'),
            "eta": dtdc_data.get('expectedDeliveryDate', '4-5 Days'),
            "history": [{"status": s.get('scanStatus'), "location": s.get('scanLocation'), "time": s.get('scanDate')} for s in details]
        }), 200

    except Exception as e:
        app.logger.error(f"Tracking Error: {str(e)}")
        return jsonify({"error": "External Tracking Service currently unavailable"}), 500

if __name__ == '__main__':
    # For local development only. Production servers use Gunicorn/Vercel.
    is_dev = os.getenv('FLASK_ENV') == 'development'
    app.run(debug=is_dev, port=5000)
