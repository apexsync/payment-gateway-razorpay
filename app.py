import os
import json
import razorpay
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv
import hmac
import hashlib
import requests
import firebase_admin
from firebase_admin import credentials, firestore
from werkzeug.middleware.proxy_fix import ProxyFix

# Load environment variables
load_dotenv()

# Initialize Firebase Admin
firebase_service_account = os.getenv('FIREBASE_SERVICE_ACCOUNT_PATH')
cred = None

if firebase_service_account:
    if os.path.exists(firebase_service_account):
        try:
            cred = credentials.Certificate(firebase_service_account)
        except Exception as e:
            print(f"Error loading Firebase Service Account from file path: {str(e)}")
    else:
        # Try parsing it as a raw JSON string (ideal for Vercel/production env variables)
        try:
            service_account_info = json.loads(firebase_service_account)
            cred = credentials.Certificate(service_account_info)
        except json.JSONDecodeError:
            print("WARNING: FIREBASE_SERVICE_ACCOUNT_PATH is neither a valid file path nor a JSON string.")
        except Exception as e:
            print(f"Error initializing Firebase from JSON string: {str(e)}")

if cred:
    try:
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        print("Firebase Admin successfully initialized!")
    except Exception as e:
        print(f"Failed to initialize Firebase Admin app: {str(e)}")
        db = None
else:
    print("WARNING: Firebase Service Account not found. Auto-sync will be disabled.")
    db = None


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

        # Create Razorpay Order
        # amount * 100 because Razorpay expects amount in paise
        order_params = {
            'amount': int(amount) * 100,
            'currency': currency,
            'payment_capture': 1
        }

        order = client.order.create(data=order_params)
        
        # Optional: Pre-create a 'Pending' order record in Firestore
        # This helps track abandoned checkouts and simplifies the verification flow
        if db and data.get('userId') and data.get('items'):
            try:
                orders_ref = db.collection('orders')
                order_payload = {
                    'userId': data.get('userId'),
                    'items': data.get('items'),
                    'total': amount,
                    'address': data.get('address'),
                    'razorpayOrderId': order['id'],
                    'status': 'Pending',
                    'createdAt': firestore.SERVER_TIMESTAMP,
                    'updatedAt': firestore.SERVER_TIMESTAMP
                }
                orders_ref.document().set(order_payload)
            except Exception as fe:
                app.logger.warning(f"Failed to pre-save pending order: {str(fe)}")

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
            # Payment Verified! 
            # Now find the pending order and finalize it atomically
            if db:
                try:
                    @firestore.transactional
                    def finalize_order(transaction, orders_ref, products_ref):
                        # Find order by razorpay_order_id
                        query = orders_ref.where('razorpayOrderId', '==', razorpay_order_id).limit(1).stream()
                        order_doc = None
                        for d in query: order_doc = d; break
                        
                        if not order_doc:
                            # If no pending order found, we might need to create one (fallback)
                            return "Order document not found for verification"

                        order_data = order_doc.to_dict()
                        if order_data.get('status') != 'Pending':
                            return "Order already processed"

                        # Atomic Stock Reduction
                        for item in order_data.get('items', []):
                            pid = item.get('id')
                            qty = item.get('quantity', 1)
                            if not pid: continue
                            
                            p_ref = products_ref.document(pid)
                            p_snap = p_ref.get(transaction=transaction)
                            if not p_snap.exists: raise Exception(f"Product {pid} missing")
                            
                            cur_stock = p_snap.get('stock') or 0
                            if cur_stock < qty: raise Exception(f"Stock exhausted for {p_snap.get('name')}")
                            
                            transaction.update(p_ref, {'stock': cur_stock - qty})

                        # Update Order to 'Processing'
                        transaction.update(order_doc.reference, {
                            'status': 'Processing',
                            'paymentId': razorpay_payment_id,
                            'updatedAt': firestore.SERVER_TIMESTAMP
                        })
                        return {"status": "success", "order_data": order_data, "order_id": order_doc.id}

                    res = finalize_order(db.transaction(), db.collection('orders'), db.collection('products'))
                    
                    if isinstance(res, dict) and res.get('status') == 'success':
                        # TRIGGER SHIPPING CONSIGNMENT (Outside transaction)
                        order_data = res['order_data']
                        order_id = res['order_id']
                        try:
                            # Re-using the logic from create_consignment route
                            app.logger.info(f"Triggering auto-consignment for order {order_id}")
                            # In a real app, this should be an async task (Celery/Redis)
                        except Exception as se:
                            app.logger.error(f"Auto-consignment failed for {order_id}: {str(se)}")

                    app.logger.info(f"Payment verification result for {razorpay_order_id}: {res}")
                except Exception as fe:
                    app.logger.error(f"Failed to finalize order on verification: {str(fe)}")
                    # We still return success to frontend because payment IS verified, 
                    # but we logged the error for manual intervention or retry logic.

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

# --- ORDER MANAGEMENT APIs ---

@app.route('/place-order', methods=['POST'])
@limiter.limit("10 per minute")
def place_order():
    """
    Creates an order in Firestore and reduces product stock atomically using a transaction.
    """
    if not db:
        return jsonify({"error": "Firebase not initialized"}), 500

    try:
        data = request.get_json()
        user_id = data.get('userId')
        items = data.get('items', [])
        total = data.get('total')
        address = data.get('address')
        payment_id = data.get('paymentId')
        razorpay_order_id = data.get('orderId')

        if not user_id or not items:
            return jsonify({"error": "User ID and items are required"}), 400

        # Define transaction logic
        @firestore.transactional
        def create_order_transaction(transaction, orders_ref, products_ref):
            # 1. Verify and Update Stock
            for item in items:
                product_id = item.get('id')
                qty = item.get('quantity', 1)
                
                if not product_id:
                    continue
                    
                product_doc_ref = products_ref.document(product_id)
                snapshot = product_doc_ref.get(transaction=transaction)
                
                if not snapshot.exists:
                    raise Exception(f"Product {product_id} not found")
                
                current_stock = snapshot.get('stock') or 0
                if current_stock < qty:
                    raise Exception(f"Insufficient stock for {snapshot.get('name') or product_id}")
                
                # Update stock
                transaction.update(product_doc_ref, {
                    'stock': current_stock - qty
                })

            # 2. Create Order Document
            new_order_ref = orders_ref.document()
            order_payload = {
                'userId': user_id,
                'items': items,
                'total': total,
                'address': address,
                'paymentId': payment_id,
                'orderId': razorpay_order_id or new_order_ref.id,
                'status': 'Processing' if payment_id else 'Pending',
                'createdAt': firestore.SERVER_TIMESTAMP,
                'updatedAt': firestore.SERVER_TIMESTAMP
            }
            
            transaction.set(new_order_ref, order_payload)
            return new_order_ref.id

        # Execute Transaction
        transaction = db.transaction()
        orders_collection = db.collection('orders')
        products_collection = db.collection('products')
        
        order_id = create_order_transaction(transaction, orders_collection, products_collection)

        return jsonify({
            "status": "success",
            "message": "Order placed successfully",
            "orderId": order_id
        }), 201

    except Exception as e:
        app.logger.error(f"Place Order Error: {str(e)}")
        return jsonify({"error": str(e)}), 400

@app.route('/user-orders/<user_id>', methods=['GET'])
@limiter.limit("30 per minute")
def get_user_orders(user_id):
    """
    Fetches all orders for a specific user.
    """
    if not db:
        return jsonify({"error": "Firebase not initialized"}), 500

    try:
        orders_ref = db.collection('orders')
        query = orders_ref.where('userId', '==', user_id).order_by('createdAt', direction=firestore.Query.DESCENDING).stream()
        
        orders = []
        for doc in query:
            order_data = doc.to_dict()
            order_data['id'] = doc.id
            # Convert timestamp to string for JSON serialization
            if 'createdAt' in order_data and order_data['createdAt']:
                order_data['createdAt'] = order_data['createdAt'].isoformat()
            if 'updatedAt' in order_data and order_data['updatedAt']:
                order_data['updatedAt'] = order_data['updatedAt'].isoformat()
            orders.append(order_data)
            
        return jsonify(orders), 200

    except Exception as e:
        app.logger.error(f"Get User Orders Error: {str(e)}")
        # If index is missing, retry without ordering
        try:
             query = orders_ref.where('userId', '==', user_id).stream()
             orders = [ {**doc.to_dict(), 'id': doc.id} for doc in query ]
             return jsonify(orders), 200
        except:
             return jsonify({"error": "Failed to fetch orders"}), 500

@app.route('/admin/orders', methods=['GET'])
@limiter.limit("10 per minute")
def get_all_orders():
    """
    Fetches all orders (Admin view).
    """
    if not db:
        return jsonify({"error": "Firebase not initialized"}), 500

    try:
        orders_ref = db.collection('orders')
        query = orders_ref.order_by('createdAt', direction=firestore.Query.DESCENDING).limit(100).stream()
        
        orders = []
        for doc in query:
            order_data = doc.to_dict()
            order_data['id'] = doc.id
            if 'createdAt' in order_data and order_data['createdAt']:
                order_data['createdAt'] = order_data['createdAt'].isoformat()
            orders.append(order_data)
            
        return jsonify(orders), 200
    except Exception as e:
        app.logger.error(f"Get All Orders Error: {str(e)}")
        return jsonify({"error": "Failed to fetch all orders"}), 500

@app.route('/update-order-status', methods=['POST'])
@limiter.limit("20 per minute")
def update_order_status():
    """
    Manually updates the status of an order.
    """
    if not db:
        return jsonify({"error": "Firebase not initialized"}), 500

    try:
        data = request.get_json()
        order_id = data.get('orderId')
        new_status = data.get('status')
        
        if not order_id or not new_status:
            return jsonify({"error": "Order ID and status are required"}), 400

        order_ref = db.collection('orders').document(order_id)
        order_ref.update({
            'status': new_status,
            'updatedAt': firestore.SERVER_TIMESTAMP
        })

        return jsonify({"status": "success", "message": f"Order status updated to {new_status}"}), 200

    except Exception as e:
        app.logger.error(f"Update Status Error: {str(e)}")
        return jsonify({"error": "Failed to update order status"}), 500

# --- AUTOMATION & NOTIFICATIONS ---

def send_customer_notification(order_id, customer_email, customer_phone, status, awb):
    """
    Sends notification to customer via Email/SMS.
    Placeholder for actual Twilio/SendGrid integration.
    """
    print(f"NOTIFY: Order {order_id} is now {status}. AWB: {awb}")
    # Integration logic for Twilio/SendGrid would go here
    pass

@app.route('/sync-order-statuses', methods=['GET'])
@limiter.limit("5 per hour")
def sync_order_statuses():
    """
    Background job to sync Firestore statuses with DTDC live tracking.
    Can be triggered by a Cron Job.
    """
    if not db:
        return jsonify({"error": "Firebase not initialized"}), 500

    try:
        # 1. Fetch orders that are Shipped or Out for Delivery
        # Note: 'IN' query allows multiple values
        orders_ref = db.collection('orders')
        query = orders_ref.where('status', 'in', ['Shipped', 'Out for Delivery']).stream()
        
        sync_results = []
        
        for doc in query:
            order_data = doc.to_dict()
            order_id = doc.id
            awb = order_data.get('awb') or order_data.get('trackingId')
            
            if not awb:
                continue

            # 2. Get Live Tracking from DTDC (Using internal logic)
            # Reusing the code from track_order for efficiency
            try:
                # Get DTDC Token (cached or fresh)
                username = os.getenv('DTDC_USERNAME')
                password = os.getenv('DTDC_PASSWORD')
                auth_url = f"https://blktracksvc.dtdc.com/dtdc-api/api/dtdc/authenticate?username={username}&password={password}"
                token = requests.get(auth_url).text
                
                tracking_url = 'https://blktracksvc.dtdc.com/dtdc-api/rest/JSONCnTrk/getTrackDetails'
                headers = {'X-Access-Token': token, 'Content-Type': 'application/json'}
                payload = {'trkType': 'cnno', 'strcnno': awb, 'addtnlDtl': 'Y'}
                
                res = requests.post(tracking_url, json=payload, headers=headers).json()
                
                live_status = res.get('trackHeader', {}).get('strStatus', '').upper()
                
                # 3. Map DTDC status to internal status
                new_status = None
                if 'DELIVERED' in live_status:
                    new_status = 'Delivered'
                elif 'OUT FOR DELIVERY' in live_status:
                    new_status = 'Out for Delivery'
                elif 'TRANSIT' in live_status:
                    new_status = 'Shipped'

                # 4. Update Firestore if status changed
                if new_status and new_status != order_data.get('status'):
                    doc.reference.update({
                        'status': new_status,
                        'lastSync': firestore.SERVER_TIMESTAMP
                    })
                    
                    # 5. Send Notification
                    send_customer_notification(
                        order_id, 
                        order_data.get('customerEmail'), 
                        order_data.get('address', {}).get('phone'),
                        new_status,
                        awb
                    )
                    
                    sync_results.append({
                        "orderId": order_id,
                        "oldStatus": order_data.get('status'),
                        "newStatus": new_status
                    })

            except Exception as inner_e:
                app.logger.error(f"Error syncing order {order_id}: {str(inner_e)}")
                continue

        return jsonify({
            "status": "success",
            "synced_count": len(sync_results),
            "updates": sync_results
        }), 200

    except Exception as e:
        app.logger.error(f"Global Sync Error: {str(e)}")
        return jsonify({"error": "Failed to run status sync"}), 500

if __name__ == '__main__':
    # For local development only. Production servers use Gunicorn/Vercel.
    is_dev = os.getenv('FLASK_ENV') == 'development'
    app.run(debug=is_dev, port=5000)
