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
    # Split by comma and strip any whitespace from each origin
    allowed_origins = [origin.strip() for origin in allowed_origins_env.split(',')]
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
        # The PDF shows trackHeader and trackDetails in the JSON output
        if not dtdc_data or 'trackHeader' not in dtdc_data:
            return jsonify({
                "error": "Tracking information is not yet available for this AWB."
            }), 404

        header = dtdc_data.get('trackHeader', {})
        details = dtdc_data.get('trackDetails', [])
        
        # Get the latest status from the header or the last item in details
        current_status = header.get('strStatus', 'In Transit')
        current_location = header.get('strDestination', 'Processing Hub')
        delivery_date = header.get('strStatusTransOn', '4-5 Days')

        return jsonify({
            "status": current_status,
            "location": current_location,
            "eta": delivery_date,
            "awb": header.get('strShipmentNo'),
            "origin": header.get('strOrigin'),
            "destination": header.get('strDestination'),
            "history": [
                {
                    "status": s.get('strAction'),
                    "location": s.get('strOrigin'),
                    "time": f"{s.get('strActionDate')} {s.get('strActionTime')}",
                    "remarks": s.get('sTrRemarks')
                } for s in details
            ]
        }), 200

    except Exception as e:
        app.logger.error(f"Tracking Error: {str(e)}")
        return jsonify({"error": "External Tracking Service currently unavailable"}), 500

@app.route('/create-consignment', methods=['POST'])
@limiter.limit("10 per minute")
def create_consignment():
    """
    Uploads order to DTDC via Shipsy Order Upload API.
    """
    try:
        data = request.get_json()
        
        shipsy_api_key = os.getenv('SHIPSY_API_KEY')
        shipsy_customer_code = os.getenv('SHIPSY_CUSTOMER_CODE')
        
        if not shipsy_api_key:
            return jsonify({"error": "Shipsy API Key not configured"}), 500

        # Construct the payload according to the PDF
        # We wrap the incoming order data into the 'consignments' array
        payload = {
            "consignments": [
                {
                    "customer_code": shipsy_customer_code,
                    "service_type_id": data.get('service_type_id', 'B2C PRIORITY'),
                    "load_type": data.get('load_type', 'NON-DOCUMENT'),
                    "description": data.get('description', 'Jewelry Order'),
                    "dimension_unit": "cm",
                    "length": str(data.get('length', '10')),
                    "width": str(data.get('width', '10')),
                    "height": str(data.get('height', '5')),
                    "weight_unit": "kg",
                    "weight": str(data.get('weight', '0.5')),
                    "declared_value": str(data.get('total')),
                    "num_pieces": "1",
                    "origin_details": {
                        "name": os.getenv('FIRM_NAME', 'SAGA'),
                        "phone": os.getenv('FIRM_PHONE', '0000000000'),
                        "address_line_1": os.getenv('FIRM_ADDRESS', 'SAGA Warehouse'),
                        "pincode": os.getenv('FIRM_PINCODE', '110001'),
                        "city": os.getenv('FIRM_CITY', 'New Delhi'),
                        "state": os.getenv('FIRM_STATE', 'Delhi')
                    },
                    "destination_details": {
                        "name": data.get('address', {}).get('fullName', 'Customer'),
                        "phone": data.get('address', {}).get('phone', '0000000000'),
                        "address_line_1": data.get('address', {}).get('addressLine', ''),
                        "address_line_2": data.get('address', {}).get('landmark', ''),
                        "pincode": data.get('address', {}).get('pincode', ''),
                        "city": data.get('address', {}).get('city', ''),
                        "state": data.get('address', {}).get('state', '')
                    },
                    "customer_reference_number": data.get('orderId'),
                    "commodity_id": "99", # Default for general goods/jewelry
                    "is_risk_surcharge_applicable": False
                }
            ]
        }

        # Shipsy API URL (Default to Production if not set)
        shipsy_url = os.getenv(
            'SHIPSY_API_URL', 
            "https://dtdcapi.shipsy.io/api/customer/integration/consignment/softdata"
        )
        headers = {
            "Content-Type": "application/json",
            "api-key": shipsy_api_key
        }

        response = requests.post(shipsy_url, json=payload, headers=headers)
        shipsy_response = response.json()

        if response.status_code == 200 and shipsy_response.get('status') == 'OK':
            # Extract the reference number (AWB) from the first consignment result
            consignment_data = shipsy_response.get('data', [{}])[0]
            if consignment_data.get('success'):
                return jsonify({
                    "status": "success",
                    "awb": consignment_data.get('reference_number'),
                    "details": consignment_data
                }), 200
            else:
                return jsonify({
                    "status": "failure",
                    "error": "Shipsy failed to process consignment",
                    "details": consignment_data
                }), 400
        
        return jsonify({
            "error": "Failed to upload order to DTDC",
            "details": shipsy_response
        }), response.status_code

    except Exception as e:
        app.logger.error(f"Consignment Error: {str(e)}")
        return jsonify({"error": "Internal error during consignment creation"}), 500

@app.route('/generate-label', methods=['GET'])
@limiter.limit("20 per minute")
def generate_label():
    """
    Generates a shipping label for a consignment via Shipsy API.
    """
    try:
        awb = request.args.get('awb')
        label_code = request.args.get('label_code', 'SHIP_LABEL_4X6')
        label_format = request.args.get('label_format', 'pdf')
        
        if not awb:
            return jsonify({"error": "AWB number is required"}), 400

        shipsy_api_key = os.getenv('SHIPSY_API_KEY')
        if not shipsy_api_key:
            return jsonify({"error": "Shipsy API Key not configured"}), 500

        # Shipsy Label API URL
        # We need to base URL and remove the specific path from create-consignment
        base_url = os.getenv('SHIPSY_API_URL', "https://dtdcapi.shipsy.io/api/customer/integration/consignment/softdata")
        # Extract the base part (e.g., https://dtdcapi.shipsy.io)
        api_base = base_url.split('/api/')[0]
        label_url = f"{api_base}/api/customer/integration/consignment/shippinglabel/stream"
        
        params = {
            'reference_number': awb,
            'label_code': label_code,
            'label_format': label_format
        }
        headers = {
            'api-key': shipsy_api_key
        }

        response = requests.get(label_url, params=params, headers=headers, stream=True)

        if response.status_code != 200:
            return jsonify({
                "error": "Failed to generate label from Shipsy",
                "details": response.text
            }), response.status_code

        # If it's a PDF, stream it back to the client
        if label_format == 'pdf':
            return (
                response.content,
                200,
                {
                    'Content-Type': 'application/pdf',
                    'Content-Disposition': f'attachment; filename=label_{awb}.pdf'
                }
            )
        
        # If it's base64, return as JSON
        return jsonify(response.json()), 200

    except Exception as e:
        app.logger.error(f"Label Generation Error: {str(e)}")
        return jsonify({"error": "Internal error during label generation"}), 500

@app.route('/cancel-consignment', methods=['POST'])
@limiter.limit("10 per minute")
def cancel_consignment():
    """
    Cancels a consignment in the DTDC/Shipsy system.
    """
    try:
        data = request.get_json()
        awb = data.get('awb')
        
        if not awb:
            return jsonify({"error": "AWB number is required"}), 400

        shipsy_api_key = os.getenv('SHIPSY_API_KEY')
        shipsy_customer_code = os.getenv('SHIPSY_CUSTOMER_CODE')
        
        if not shipsy_api_key:
            return jsonify({"error": "Shipsy API Key not configured"}), 500

        # Shipsy Cancellation API URL
        base_url = os.getenv('SHIPSY_API_URL', "https://dtdcapi.shipsy.io/api/customer/integration/consignment/softdata")
        api_base = base_url.split('/api/')[0]
        cancel_url = f"{api_base}/api/customer/integration/consignment/cancel"
        
        payload = {
            "AWBNo": [str(awb)],
            "customerCode": shipsy_customer_code
        }
        headers = {
            "Content-Type": "application/json",
            "api-key": shipsy_api_key
        }

        response = requests.post(cancel_url, json=payload, headers=headers)
        shipsy_response = response.json()

        if response.status_code == 200 and shipsy_response.get('status') == 'OK':
            # Check if the specific consignment was canceled
            consignments = shipsy_response.get('successConsignments', [])
            if consignments and consignments[0].get('success'):
                return jsonify({
                    "status": "success",
                    "message": "Consignment canceled successfully",
                    "details": shipsy_response
                }), 200
            else:
                return jsonify({
                    "status": "failure",
                    "error": "Shipsy failed to cancel consignment",
                    "details": shipsy_response
                }), 400
        
        return jsonify({
            "error": "Failed to cancel order with DTDC",
            "details": shipsy_response
        }), response.status_code

    except Exception as e:
        app.logger.error(f"Cancellation Error: {str(e)}")
        return jsonify({"error": "Internal error during cancellation"}), 500

@app.route('/refund-payment', methods=['POST'])
@limiter.limit("10 per minute")
def refund_payment():
    """
    Issues a refund for a payment via Razorpay.
    """
    try:
        data = request.get_json()
        payment_id = data.get('paymentId')
        amount = data.get('amount') # Optional: Amount in rupees. If not provided, full refund is issued.
        
        if not payment_id:
            return jsonify({"error": "Payment ID is required"}), 400

        # Construct refund data
        refund_data = {
            "payment_id": payment_id
        }
        
        # If amount is provided, convert to paise for Razorpay
        if amount:
            refund_data["amount"] = int(float(amount) * 100)

        # Issue Refund via Razorpay Client
        refund = client.payment.refund(payment_id, refund_data)
        
        return jsonify({
            "status": "success",
            "message": "Refund issued successfully",
            "refund_id": refund.get('id'),
            "details": refund
        }), 200

    except Exception as e:
        app.logger.error(f"Refund Error: {str(e)}")
        # Check for specific Razorpay errors
        error_msg = str(e)
        if "already been refunded" in error_msg:
            return jsonify({"error": "This payment has already been refunded"}), 400
        return jsonify({"error": f"Failed to issue refund: {error_msg}"}), 500

if __name__ == '__main__':
    # For local development only. Production servers use Gunicorn/Vercel.
    is_dev = os.getenv('FLASK_ENV') == 'development'
    app.run(debug=is_dev, port=5000)
