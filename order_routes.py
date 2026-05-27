import os
import requests
from flask import Blueprint, request, jsonify
from firebase_admin import firestore
from config import db, limiter, app

order_bp = Blueprint('order', __name__)

def send_customer_notification(order_id, customer_email, customer_phone, status, awb):
    """
    Sends notification to customer via Email/SMS.
    Placeholder for actual Twilio/SendGrid integration.
    """
    print(f"NOTIFY: Order {order_id} is now {status}. AWB: {awb}")
    # Integration logic for Twilio/SendGrid would go here
    pass

@order_bp.route('/place-order', methods=['POST'])
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

@order_bp.route('/user-orders/<user_id>', methods=['GET'])
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
             orders_ref = db.collection('orders')
             query = orders_ref.where('userId', '==', user_id).stream()
             orders = [ {**doc.to_dict(), 'id': doc.id} for doc in query ]
             # Convert timestamps in fallback
             for o in orders:
                 if 'createdAt' in o and o['createdAt']:
                     o['createdAt'] = o['createdAt'].isoformat()
                 if 'updatedAt' in o and o['updatedAt']:
                     o['updatedAt'] = o['updatedAt'].isoformat()
             return jsonify(orders), 200
        except Exception as fallback_e:
             app.logger.error(f"Fallback Get User Orders Error: {str(fallback_e)}")
             return jsonify({"error": "Failed to fetch orders"}), 500

@order_bp.route('/admin/orders', methods=['GET'])
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
            if 'updatedAt' in order_data and order_data['updatedAt']:
                order_data['updatedAt'] = order_data['updatedAt'].isoformat()
            orders.append(order_data)
            
        return jsonify(orders), 200
    except Exception as e:
        app.logger.error(f"Get All Orders Error: {str(e)}")
        return jsonify({"error": "Failed to fetch all orders"}), 500

@order_bp.route('/update-order-status', methods=['POST'])
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


