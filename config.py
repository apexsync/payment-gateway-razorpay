import os
import json
import razorpay
from flask import Flask
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore
from werkzeug.middleware.proxy_fix import ProxyFix

# Load environment variables
load_dotenv(override=True)

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
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
        else:
            firebase_admin.get_app()
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
    allowed_origins = ["http://localhost:5173", "http://localhost:5174", "http://localhost:5175", "http://localhost:3000", "http://localhost:3001"]

CORS(app, origins=allowed_origins, supports_credentials=True)
print(f"CORS Allowed Origins: {allowed_origins}")

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
