import os
from flask import jsonify
from config import app
from payment_routes import payment_bp
from order_routes import order_bp

# Register Blueprints
app.register_blueprint(payment_bp)
app.register_blueprint(order_bp)

@app.route('/', methods=['GET'])
def home():
    """
    Root status check.
    """
    return jsonify({"status": "Saga Core Backend is Running"}), 200

if __name__ == '__main__':
    # For local development only. Production servers use Gunicorn/Vercel.
    is_dev = os.getenv('FLASK_ENV') == 'development'
    app.run(debug=is_dev, port=5000)

